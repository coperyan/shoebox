/**
 * Cloud Run deployment for the scheduled shoebox pipelines.
 *
 * Scope: the pipelines that talk to eBay, GCS, BigQuery and Slack and need
 * nothing from a local disk. The Chrome-driven pipelines (create-listings with
 * price scraping, relist-listings, sync-topps-calendar, tcdb-search) and the
 * Streamlit UI stay on a workstation -- they read card scans and Excel
 * workbooks from local paths and drive a real browser. docs/deployment.md has
 * the full split.
 *
 * The job list below is the cloud counterpart of scripts/tasks.yaml, which
 * remains the source of truth for the Windows Task Scheduler setup. Keeping
 * both means a schedule is reviewable in the repo on either host.
 */

terraform {
  required_version = ">= 1.5"
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
}

locals {
  # A Cloud Run secret volume mounts a directory, not a file, so each secret
  # gets its own directory and the file lands inside it. The app is told where
  # to look: SHOEBOX_CONFIG_PATH and EBAY_REST_CONFIG_PATH are read directly by
  # settings.py and clients/ebay/session.py, and app.yaml itself carries the
  # paths to the other two.
  secret_dir = { for key, cfg in var.secrets : key => "/secrets/${replace(key, "_", "-")}" }
  secret_file = {
    for key, cfg in var.secrets : key => "${local.secret_dir[key]}/${cfg.filename}"
  }

  # Environment shared by every container: console-only logging (the platform
  # collects stdout, and the container filesystem is discarded), plus the two
  # config locations.
  common_env = {
    SHOEBOX_LOG_DIR       = "-"
    SHOEBOX_CONFIG_PATH   = local.secret_file["app"]
    EBAY_REST_CONFIG_PATH = local.secret_file["ebay_rest"]
  }

  # One entry per scheduled pipeline: the command to run and when.
  #
  # Timeouts are generous but finite. sync-active-listing-details fans out over
  # every active listing via the Trading API and is by far the longest, which is
  # why it gets an hour while the rest get fifteen minutes.
  jobs = {
    "sync-active-listings" = {
      args     = ["sync-active-listings"]
      schedule = "0 3 * * *"
      timeout  = "1800s"
      memory   = "1Gi"
    }
    "sync-active-listing-details" = {
      args     = ["sync-active-listing-details"]
      schedule = "30 3 * * *"
      timeout  = "3600s"
      memory   = "2Gi"
    }
    "sync-orders" = {
      args     = ["sync-orders"]
      schedule = "0 4 * * *"
      timeout  = "1800s"
      memory   = "1Gi"
    }
    "end-oos-listings" = {
      args     = ["end-oos-listings"]
      schedule = "0 5 * * *"
      timeout  = "900s"
      memory   = "512Mi"
    }
    "watch-searches" = {
      args     = ["watch-searches"]
      schedule = var.watch_searches_schedule
      timeout  = "900s"
      memory   = "512Mi"
    }
  }

  # orders-awaiting-shipment runs two commands in sequence, the same pair the
  # Windows task runs. It is manual there and stays unscheduled here: it is a
  # "I am about to go pack orders" command, not a clock-driven one. Trigger it
  # from Slack, or with `gcloud run jobs execute`.
  manual_jobs = {
    "orders-awaiting-shipment-pull" = {
      args    = ["orders-awaiting-shipment", "--pull-order", "--message"]
      timeout = "900s"
      memory  = "512Mi"
    }
    "orders-awaiting-shipment-buyer" = {
      args    = ["orders-awaiting-shipment", "--buyer-order", "--message"]
      timeout = "900s"
      memory  = "512Mi"
    }
  }

  all_jobs = merge(
    { for name, job in local.jobs : name => job },
    { for name, job in local.manual_jobs : name => job },
  )
}

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

# This replaces configs/gcp.json. The clients fall back to Application Default
# Credentials when that file is absent, so mounting no key and attaching this
# account is both simpler and safer than shipping a downloaded key.
resource "google_service_account" "runner" {
  account_id   = "shoebox-runner"
  display_name = "shoebox scheduled pipelines"
  description  = "Runs the shoebox Cloud Run jobs; replaces the downloaded gcp.json key."
}

resource "google_project_iam_member" "bigquery_user" {
  project = var.project_id
  role    = "roles/bigquery.user"
  member  = "serviceAccount:${google_service_account.runner.email}"
}

resource "google_project_iam_member" "bigquery_data_editor" {
  project = var.project_id
  role    = "roles/bigquery.dataEditor"
  member  = "serviceAccount:${google_service_account.runner.email}"
}

# Object-level access only; the pipelines create no buckets.
resource "google_project_iam_member" "storage_object_admin" {
  project = var.project_id
  role    = "roles/storage.objectAdmin"
  member  = "serviceAccount:${google_service_account.runner.email}"
}

resource "google_secret_manager_secret_iam_member" "runner_access" {
  for_each  = var.secrets
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runner.email}"
}

# Separate identity for Cloud Scheduler, so the thing that *triggers* a job
# cannot also read the store's credentials.
resource "google_service_account" "scheduler" {
  account_id   = "shoebox-scheduler"
  display_name = "shoebox scheduler"
  description  = "Invokes the shoebox Cloud Run jobs on a schedule."
}

resource "google_cloud_run_v2_job_iam_member" "scheduler_invoker" {
  for_each = local.jobs
  name     = google_cloud_run_v2_job.pipeline[each.key].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler.email}"
}

# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_job" "pipeline" {
  for_each = local.all_jobs

  name                = each.key
  location            = var.region
  deletion_protection = false

  template {
    # No retries. These pipelines append snapshot rows to BigQuery keyed by
    # file_date, so a retry after a partial load would double-count rather than
    # heal. A failed run is better re-run deliberately once the cause is known.
    task_count  = 1
    parallelism = 1

    template {
      service_account = google_service_account.runner.email
      timeout         = each.value.timeout
      max_retries     = 0

      containers {
        image = var.image
        args  = each.value.args

        resources {
          limits = {
            cpu    = "1"
            memory = each.value.memory
          }
        }

        dynamic "env" {
          for_each = local.common_env
          content {
            name  = env.key
            value = env.value
          }
        }

        dynamic "volume_mounts" {
          for_each = var.secrets
          content {
            name       = replace(volume_mounts.key, "_", "-")
            mount_path = local.secret_dir[volume_mounts.key]
          }
        }
      }

      dynamic "volumes" {
        for_each = var.secrets
        content {
          name = replace(volumes.key, "_", "-")
          secret {
            secret = volumes.value.secret_id
            items {
              version = "latest"
              path    = volumes.value.filename
              mode    = 0400
            }
          }
        }
      }
    }
  }
}

# ---------------------------------------------------------------------------
# Schedules
# ---------------------------------------------------------------------------

resource "google_cloud_scheduler_job" "pipeline" {
  for_each = local.jobs

  name      = each.key
  region    = var.region
  schedule  = each.value.schedule
  time_zone = var.timezone

  # A missed tick is not worth piling up: the daily syncs are snapshots and the
  # watcher re-checks in five minutes regardless.
  attempt_deadline = "320s"

  retry_config {
    retry_count = 0
  }

  http_target {
    http_method = "POST"
    uri = join("", [
      "https://${var.region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/",
      "${var.project_id}/jobs/${google_cloud_run_v2_job.pipeline[each.key].name}:run",
    ])

    oauth_token {
      service_account_email = google_service_account.scheduler.email
    }
  }
}

# ---------------------------------------------------------------------------
# Slack bot (always-on service)
# ---------------------------------------------------------------------------

resource "google_cloud_run_v2_service" "slack_bot" {
  count = var.enable_bot ? 1 : 0

  name                = "shoebox-slack-bot"
  location            = var.region
  deletion_protection = false

  # Socket Mode dials out to Slack; nothing on the internet needs to reach this.
  ingress = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  template {
    service_account = google_service_account.runner.email

    scaling {
      # Exactly one instance: the bot holds a WebSocket and shells out to the
      # CLI, so a second instance would run every chat command twice.
      min_instance_count = 1
      max_instance_count = 1
    }

    containers {
      image = var.image
      args  = ["slack-bot"]

      resources {
        limits = {
          cpu    = "1"
          memory = "1Gi"
        }
        # A Socket Mode listener does its work between requests, and Cloud Run
        # throttles CPU outside a request unless told otherwise. Without this
        # the bot stops responding shortly after each command.
        cpu_idle = false
      }

      ports {
        container_port = 8080
      }

      dynamic "env" {
        for_each = merge(local.common_env, {
          PORT = "8080"
          # Withhold the commands this host cannot service rather than letting
          # them fail in a Slack thread minutes later.
          SHOEBOX_BOT_COMMANDS = var.bot_commands
        })
        content {
          name  = env.key
          value = env.value
        }
      }

      startup_probe {
        http_get {
          path = "/healthz"
        }
        initial_delay_seconds = 5
        period_seconds        = 5
        failure_threshold     = 6
      }

      dynamic "volume_mounts" {
        for_each = var.secrets
        content {
          name       = replace(volume_mounts.key, "_", "-")
          mount_path = local.secret_dir[volume_mounts.key]
        }
      }
    }

    dynamic "volumes" {
      for_each = var.secrets
      content {
        name = replace(volumes.key, "_", "-")
        secret {
          secret = volumes.value.secret_id
          items {
            version = "latest"
            path    = volumes.value.filename
            mode    = 0400
          }
        }
      }
    }
  }
}
