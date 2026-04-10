output "project_id" {
  value = var.project_id
}

output "region" {
  value = var.region
}

output "zone" {
  value = var.zone
}

output "cluster_name" {
  value = google_container_cluster.benchmark.name
}

output "cluster_location" {
  value = google_container_cluster.benchmark.location
}

output "gke_get_credentials_command" {
  value = "gcloud container clusters get-credentials ${google_container_cluster.benchmark.name} --zone ${google_container_cluster.benchmark.location} --project ${var.project_id}"
}

output "gpu_node_pool_name" {
  value = google_container_node_pool.gpu.name
}

output "gcs_bucket_name" {
  value = google_storage_bucket.models.name
}

output "vpc_network_name" {
  value = google_compute_network.gke.name
}

output "subnetwork_name" {
  value = google_compute_subnetwork.gke.name
}
