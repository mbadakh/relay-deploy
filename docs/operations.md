# Operations and recovery

## Before deployment

Use an isolated AWS workload account and a short-lived SSO session. Install Git and Docker; on Windows use WSL2 with Docker integration. The `relay` wrapper runs pinned tools in an amd64 container (Docker Desktop can emulate this on Apple Silicon). Only the checkout and your selected AWS configuration directory are mounted; the Docker socket is not mounted.

`AWS_CONFIG_DIRECTORY` selects an alternative credentials directory. `./relay aws ...` may update that directory for SSO configuration/login; deployments mount it read-only. Refresh an expired SSO session with `./relay aws sso login --profile NAME` before continuing. External `credential_process` executables must be available inside the tools container; export short-lived session credentials if your local helper is not portable.

The operator needs provisioning access in their own account. The saved plan shows IAM, network, storage, database and compute changes. No central GitHub trust role, parent DNS delegation, Firebase key, or cross-account management role is installed.

## Plan and state

`.relay/NAME/` is mode 0700 and contains private Terraform state, saved plans, configuration, pricing and image locks. Back it up encrypted outside the checkout. Never commit or share raw state or plan JSON. The HTML report omits raw values and secrets, but contains account and resource metadata. Do not publish it without reviewing it.

Only one local operation can hold the deployment lock. Do not operate copies of the same state simultaneously. For a team, migrate state to your own encrypted, access-controlled remote backend with locking before sharing operations. A deleted local state file does not delete AWS resources or stop billing.

`./relay plan ...` only reads AWS and creates local files. `./relay deploy ...` saves an exact plan, prices it, generates HTML and requires `APPLY <fingerprint>`. There is no auto-approve flag. New resource types unknown to the estimator stop deployment. Estimates exclude taxes and can differ from actual usage.

## Resume and upgrade

```sh
./relay status --name my-relay
./relay resume --name my-relay
```

Resume uses the deployment's saved application image lock, regenerates its runtime manifests, preserves existing AWS secrets, and reconciles the deployment. It does not approve an unapplied Terraform plan. If infrastructure apply failed partway, rerun `deploy` with the original options and review the new plan.

For a deliberate upgrade, back up state and application data, fetch a reviewed deployment repository version, inspect its image-lock changes, then rerun `deploy` with the same name, region and configuration. Review its plan even if it has no infrastructure changes. Save the previous checkout and image lock for rollback. Database migrations can make image rollback unsafe: restore a tested database backup when necessary.

Customer images are mirrored from public ECR into their own immutable ECR repositories. Runtime instances use their own IAM roles; no GitHub login is required. Image digests are verified before and after copying.

## Public URL and TLS

Without a domain, AWS returns a public IP (home lab) or load balancer DNS name (production). Its initial HTTP URL is for setup behind your HTTPS reverse proxy. Forward `/`, `/api`, `/socket.io` and `/auth` to the endpoint; preserve Host and set trusted `X-Forwarded-Proto: https`. Support WebSocket upgrades. Restrict the origin security group to your proxy where possible.

Then run:

```sh
./relay configure-url --name my-relay --public-url https://chat.example.com
```

The command updates the existing Keycloak client through SSM, updates customer-owned secrets, and reconciles services. A hostname change signs users out and requires push enrollment again. Update your proxy first. If you originally supplied a Route 53 zone for managed TLS, changing the domain also needs a reviewed `deploy` plan to update DNS/certificates; `configure-url` is for an external reverse proxy, not a replacement for those Terraform changes.

The mobile app and push gateway require trusted public HTTPS on port 443 and a public IPv4 origin. Localhost and LAN-only origins do not support background Android push through the public gateway.

## Local configuration, backup and removal

Copy `.env.example` to `.env` **before the first start** to change port, public URL, bind address, administrator email and advertised TURN URL. For remote clients set a reachable host address and TLS proxy. Open TURN TCP/UDP 3478 and UDP 49160–49200 to clients and configure NAT port forwarding. Loopback defaults are for a single computer, not Internet calling.

Local volumes contain PostgreSQL, media, Keycloak data, generated secrets and TLS state. Use PostgreSQL's dump/restore tools and an object-storage backup; take consistent encrypted volume backups during a maintenance stop. Test restoration in a separate Compose project. Do not copy a running database directory as your only backup.

```sh
docker compose stop        # preserve data
docker compose up -d       # resume
docker compose down       # remove containers, preserve named volumes
```

The first-run origin is deliberately locked. For an existing local installation, preserve volumes, update the Keycloak client's redirect and web origins in its admin console, and update `relay.env`, `keycloak.env`, `relay-realm.json` and `origin` in the private config volume before recreating containers. Do not delete that volume to change a URL: it also holds database credentials.

## AWS deletion

First export and verify any data/backups you need to retain outside the managed deployment. Then:

```sh
./relay destroy --name my-relay --purge-data
```

This requires `PURGE NAME`, approval of an exact preparation plan that disables deletion protections, and approval of an exact destroy plan. It permanently removes managed databases, media, EBS volumes and managed backup contents. It does not create a final database snapshot in purge mode. KMS keys and secrets retain AWS's configured recovery windows. Copies you exported independently are not deleted. If interrupted, rerun the command and inspect the remaining-resource plan. Check your AWS console and billing after removal.

## CI and publication

All jobs use repository variable `RELAY_RUNNER_LABELS`, defaulting to `["self-hosted","linux","x64","relay-local"]`. CI accepts trusted branch pushes and manual dispatches; it does not execute public-fork PRs on the privileged runner pool. Future workflows should use this same expression. No PRs are required by these pipelines.

Public-image publishing requires the narrowly scoped OIDC role described in `ci/public-publisher-iam.json`; its creation is pending explicit approval after automatic approval review blocked it. Set `PUBLIC_ECR_PUBLISH_ROLE_ARN` only after that role is approved and installed. No AWS access key or fallback to the runner's ambient credentials is used. `registry.r3l4y.dev` is an image catalog; Docker pulls use the actual `public.ecr.aws` names because a DNS CNAME alone cannot provide a valid custom Docker registry endpoint.
