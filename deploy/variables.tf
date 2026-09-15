variable "project_id" {
  type        = string
  description = "GCP project that owns the jobs, secrets, and the existing GCS/BigQuery resources."
}

variable "region" {
  type        = string
  description = "Region for Cloud Run and Cloud Scheduler."
  default     = "us-central1"
}

variable "image" {
  type        = string
  description = <<-EOT
    Fully qualified image to run, ideally pinned by digest
    (REGION-docker.pkg.dev/PROJECT/REPO/shoebox@sha256:...).

    Pinning by digest rather than :latest is what makes a deploy an explicit
    act: a Cloud Build run then produces an image without silently changing
    what the next scheduled execution runs.
  EOT
}

variable "timezone" {
  type        = string
  description = <<-EOT
    IANA timezone for the schedules. The Windows tasks these replace fire at
    local wall-clock times, so set your own zone rather than converting to UTC
    -- Cloud Scheduler handles daylight saving, a hand-converted UTC cron does
    not.
  EOT
  default     = "America/Los_Angeles"
}

variable "watch_searches_schedule" {
  type        = string
  description = "Cron for the saved-search watcher. Each search's own interval still gates it."
  default     = "*/5 * * * *"
}

variable "bot_commands" {
  type        = string
  description = <<-EOT
    Comma-separated commands the deployed Slack bot may run
    (SHOEBOX_BOT_COMMANDS). Deliberately excludes create-listings,
    create-variation-listings and create-queue-excel: those read card scans and
    Excel workbooks from local disk and drive Chrome, none of which exist in the
    container. Leave them to the workstation.
  EOT
  default     = "sync-metadata,sync-orders,sync-active-listings,sync-active-listing-details,end-oos-listings,orders-awaiting-shipment,watch-searches"
}

variable "enable_bot" {
  type        = bool
  description = <<-EOT
    Deploy the Socket Mode bot as an always-on Cloud Run service. This is the
    one component billed continuously (min_instance_count = 1 with CPU always
    allocated), because a Socket Mode connection has no inbound requests to
    scale on. Set false to keep the bot on a workstation.
  EOT
  default     = true
}

variable "secrets" {
  type = map(object({
    secret_id = string
    filename  = string
  }))
  description = <<-EOT
    Secret Manager secrets for the gitignored config files, one per map entry.

    A Cloud Run secret volume mounts a *directory*, so each secret gets its own
    directory at /secrets/<key> and appears inside it under `filename`. The
    "app" and "ebay_rest" keys are required: their paths are handed to the app
    through SHOEBOX_CONFIG_PATH and EBAY_REST_CONFIG_PATH. The remaining two are
    located by app.yaml itself (ebay.trading_token_path and paths.searches_file)
    -- see deploy/README.md for the values those need.

    gcp.json is absent on purpose: GCSClient and BigQueryClient fall back to
    Application Default Credentials when the file is missing, so the job's
    service account replaces the key file entirely and there is one fewer
    credential to rotate.
  EOT
  default = {
    app = {
      secret_id = "shoebox-app-yaml"
      filename  = "app.yaml"
    }
    ebay_rest = {
      secret_id = "shoebox-ebay-rest"
      filename  = "ebay_rest.json"
    }
    ebay_legacy = {
      secret_id = "shoebox-ebay-legacy"
      filename  = "ebay_legacy.json"
    }
    searches = {
      secret_id = "shoebox-searches"
      filename  = "searches.yaml"
    }
  }

  validation {
    condition     = alltrue([for k in ["app", "ebay_rest"] : contains(keys(var.secrets), k)])
    error_message = "secrets must define the \"app\" and \"ebay_rest\" keys; the container is pointed at them by environment variable."
  }
}
