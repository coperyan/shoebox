output "runner_service_account" {
  description = "Grant this account access to the GCS buckets and BigQuery datasets named in app.yaml. It replaces configs/gcp.json."
  value       = google_service_account.runner.email
}

output "scheduler_service_account" {
  description = "Identity Cloud Scheduler uses to trigger the jobs."
  value       = google_service_account.scheduler.email
}

output "scheduled_jobs" {
  description = "Cloud Run jobs with a schedule, and the cron driving each."
  value       = { for name, job in local.jobs : name => job.schedule }
}

output "manual_jobs" {
  description = "Jobs with no schedule; run them with `gcloud run jobs execute NAME --region REGION`."
  value       = keys(local.manual_jobs)
}

output "config_paths" {
  description = "Where each config file lands in the container. app.yaml must point ebay.trading_token_path and paths.searches_file at these."
  value       = local.secret_file
}

output "slack_bot_url" {
  description = "Internal URL of the bot service, if deployed. Nothing needs to call it; Socket Mode dials out."
  value       = var.enable_bot ? google_cloud_run_v2_service.slack_bot[0].uri : null
}
