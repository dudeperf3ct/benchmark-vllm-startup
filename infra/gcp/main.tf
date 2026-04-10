terraform {
  required_version = ">= 1.8.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

data "google_project" "current" {
  project_id = var.project_id
}

locals {
  node_compute_sa_email  = "${data.google_project.current.number}-compute@developer.gserviceaccount.com"
  llm_bench_wi_principal = "principal://iam.googleapis.com/projects/${data.google_project.current.number}/locations/global/workloadIdentityPools/${var.project_id}.svc.id.goog/subject/ns/llm-bench/sa/default"
}

resource "google_project_service" "services" {
  for_each = toset([
    "cloudresourcemanager.googleapis.com",
    "compute.googleapis.com",
    "container.googleapis.com",
    "containerfilesystem.googleapis.com",
    "storagetransfer.googleapis.com",
    "storage.googleapis.com",
  ])

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_compute_network" "gke" {
  name                    = var.network_name
  auto_create_subnetworks = false

  depends_on = [google_project_service.services]
}

resource "google_compute_subnetwork" "gke" {
  name          = var.subnetwork_name
  region        = var.region
  network       = google_compute_network.gke.id
  ip_cidr_range = var.subnetwork_cidr

  secondary_ip_range {
    range_name    = var.pods_secondary_range_name
    ip_cidr_range = var.pods_secondary_cidr
  }

  secondary_ip_range {
    range_name    = var.services_secondary_range_name
    ip_cidr_range = var.services_secondary_cidr
  }
}

resource "google_container_cluster" "benchmark" {
  name                     = var.cluster_name
  location                 = var.zone
  network                  = google_compute_network.gke.id
  subnetwork               = google_compute_subnetwork.gke.id
  remove_default_node_pool = true
  initial_node_count       = 1
  deletion_protection      = false

  release_channel {
    channel = var.release_channel
  }

  ip_allocation_policy {
    cluster_secondary_range_name  = var.pods_secondary_range_name
    services_secondary_range_name = var.services_secondary_range_name
  }

  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  depends_on = [
    google_project_service.services,
    google_compute_subnetwork.gke,
  ]
}

resource "google_container_node_pool" "gpu" {
  name       = var.gpu_node_pool_name
  cluster    = google_container_cluster.benchmark.name
  location   = var.zone
  node_count = var.gpu_node_count

  management {
    auto_repair  = true
    auto_upgrade = true
  }

  upgrade_settings {
    max_surge       = 0
    max_unavailable = 1
  }

  node_config {
    machine_type = var.gpu_machine_type
    disk_size_gb = var.gpu_disk_size_gb
    disk_type    = var.gpu_disk_type
    image_type   = var.gpu_image_type

    gcfs_config {
      enabled = var.gpu_enable_image_streaming
    }

    oauth_scopes = ["https://www.googleapis.com/auth/cloud-platform"]
    labels = {
      workload = "llm-benchmark"
      pool     = var.gpu_node_pool_name
    }

    guest_accelerator {
      type  = var.gpu_type
      count = var.gpu_count
    }

    metadata = {
      disable-legacy-endpoints = "true"
    }
  }

  depends_on = [google_container_cluster.benchmark]
}

resource "google_storage_bucket" "models" {
  name                        = var.gcs_bucket_name
  location                    = var.bucket_location
  uniform_bucket_level_access = true
  force_destroy               = var.bucket_force_destroy

  depends_on = [google_project_service.services]
}

resource "google_storage_bucket_iam_member" "models_compute_object_viewer" {
  bucket = google_storage_bucket.models.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${local.node_compute_sa_email}"
}

resource "google_storage_bucket_iam_member" "models_compute_bucket_reader" {
  bucket = google_storage_bucket.models.name
  role   = "roles/storage.legacyBucketReader"
  member = "serviceAccount:${local.node_compute_sa_email}"
}

resource "google_storage_bucket_iam_member" "models_wi_object_viewer" {
  bucket = google_storage_bucket.models.name
  role   = "roles/storage.objectViewer"
  member = local.llm_bench_wi_principal
}

resource "google_storage_bucket_iam_member" "models_wi_bucket_reader" {
  bucket = google_storage_bucket.models.name
  role   = "roles/storage.legacyBucketReader"
  member = local.llm_bench_wi_principal
}
