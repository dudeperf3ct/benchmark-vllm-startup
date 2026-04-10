variable "project_id" {
  description = "GCP project ID."
  type        = string
}

variable "region" {
  description = "GCP region for networking and bucket resources."
  type        = string
  default     = "europe-west4"
}

variable "zone" {
  description = "GCP zone for the zonal GKE cluster and GPU node pool."
  type        = string
  default     = "europe-west4-a"
}

variable "cluster_name" {
  description = "Name of the GKE cluster."
  type        = string
  default     = "benchmark-cold-start"
}

variable "release_channel" {
  description = "GKE release channel."
  type        = string
  default     = "REGULAR"
}

variable "network_name" {
  description = "Name of the VPC network for GKE."
  type        = string
  default     = "benchmark-cold-start-vpc"
}

variable "subnetwork_name" {
  description = "Name of the subnetwork for GKE nodes."
  type        = string
  default     = "benchmark-cold-start-subnet"
}

variable "subnetwork_cidr" {
  description = "Primary CIDR for GKE nodes."
  type        = string
  default     = "10.10.0.0/20"
}

variable "pods_secondary_range_name" {
  description = "Secondary range name for pod IPs."
  type        = string
  default     = "gke-pods"
}

variable "pods_secondary_cidr" {
  description = "Secondary CIDR for pod IPs."
  type        = string
  default     = "10.20.0.0/16"
}

variable "services_secondary_range_name" {
  description = "Secondary range name for service IPs."
  type        = string
  default     = "gke-services"
}

variable "services_secondary_cidr" {
  description = "Secondary CIDR for service IPs."
  type        = string
  default     = "10.30.0.0/20"
}

variable "gpu_node_pool_name" {
  description = "Name of the GPU node pool."
  type        = string
  default     = "gpu-pool"
}

variable "gpu_node_count" {
  description = "Number of GPU nodes."
  type        = number
  default     = 1
}

variable "gpu_machine_type" {
  description = "Machine type for the GPU node pool."
  type        = string
  default     = "g2-standard-8"
}

variable "gpu_type" {
  description = "GPU accelerator type."
  type        = string
  default     = "nvidia-l4"
}

variable "gpu_count" {
  description = "Number of GPUs per node."
  type        = number
  default     = 1
}

variable "gpu_disk_size_gb" {
  description = "Boot disk size for GPU nodes."
  type        = number
  default     = 200
}

variable "gpu_disk_type" {
  description = "Boot disk type for GPU nodes."
  type        = string
  default     = "pd-balanced"
}

variable "gpu_image_type" {
  description = "GKE node image type. COS_CONTAINERD is the simplest default."
  type        = string
  default     = "COS_CONTAINERD"
}

variable "gpu_enable_image_streaming" {
  description = "Enable GKE image streaming (gcfs) on the GPU node pool."
  type        = bool
  default     = false
}

variable "gcs_bucket_name" {
  description = "Globally unique GCS bucket name for model artifacts."
  type        = string
}

variable "bucket_location" {
  description = "Bucket location. Use the same region as the cluster when possible."
  type        = string
  default     = "EUROPE-WEST4"
}

variable "bucket_force_destroy" {
  description = "Allow destroying a non-empty bucket. Useful for throwaway benchmark environments."
  type        = bool
  default     = false
}
