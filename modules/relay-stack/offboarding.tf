# Terraform refresh-only plans cannot evaluate ordinary resource references
# after a previous destroy attempt has already removed those instances from
# state. This deliberately empty built-in resource gives the lifecycle workflow
# a target for a saved, no-op destroy-mode preparation plan in that recovery
# case. It creates no provider-side or Terraform-state object.
resource "terraform_data" "offboarding_recovery_noop" {
  count = 0
}
