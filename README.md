# Relay — deploy it yourself

**Private preview.** This repository and its instruction page remain private until the owner approves publication. GitHub access is needed to clone it. Runtime images are downloadable from public ECR.

**Release gate:** the compatible new production image is built by Relay’s release pipeline. Automated public publishing is pending approval of the narrow OIDC role. Until that image is published and adopted, `deploy` and local initialization stop with a clear message; `./relay plan ...` and the private instructions preview work now. This prevents deploying the older public image with incompatible startup/authentication behavior.

## Local Docker

Install Git and Docker with Compose, then:

```sh
git clone https://github.com/mbadakh/relay-deploy.git
cd relay-deploy
docker compose up -d
```

Open http://localhost:8080 after startup. Obtain your unique temporary administrator password:

```sh
docker compose run --rm init cat /run/relay/admin.txt
```

The complete suite includes Relay, Keycloak, PostgreSQL, MinIO, Caddy and TURN. Credentials are generated once and kept in a private Docker volume. Restarting preserves them and your data. Do not use `down -v` unless you intend to delete that data.

## AWS

The terminal uses **your AWS account**, not Relay’s account. It does not create a cross-account role or give Relay access. Use an isolated workload account and a short-lived SSO session. The initial operator needs permission to create the resources shown in the plan, including IAM roles and policies; runtime roles have narrower permissions.

```sh
./relay aws configure sso
./relay aws sso login --profile YOUR_PROFILE
./relay --profile YOUR_PROFILE
```

Or skip the questions:

```sh
./relay deploy --home-lab --name my-relay --region eu-central-1 \
  --admin-email you@example.com --profile YOUR_PROFILE --non-interactive
```

Production example:

```sh
./relay deploy --production --users 1k --name company-relay \
  --region eu-central-1 --admin-email you@example.com \
  --public-url https://chat.example.com --zone-id YOUR_ROUTE53_ZONE_ID
```

The installer creates a saved Terraform plan, looks up current AWS prices, and writes `.relay/SERVER/review.html`. Open that report and type the plan fingerprint in the terminal to approve. `--non-interactive` skips configuration questions, **never plan approval**. Use `./relay plan ...` for an estimate without applying.

At completion it prints the public IP/load balancer hostname, app URL, TURN address and the ARN containing the initial administrator password. `./relay admin --name SERVER` displays that password after a separate terminal confirmation.

No domain is required to create infrastructure. An HTTP endpoint is useful for connecting your own reverse proxy; **trusted public HTTPS is required for the official mobile app and gateway enrollment**. Configure that URL with `./relay configure-url --name SERVER --public-url https://chat.example.com`. See [operations](docs/operations.md).

## Instruction page

Open [docs/index.html](docs/index.html) locally for the Local/AWS tabs, sizing controls and generated command. It makes no network requests or cloud changes. See example [home-lab](docs/example-home-lab-review.html) and [production](docs/example-production-review.html) review reports. The pricing report uses the same styles as the previous web pricing page.

## Notifications and the shared Android app

Customer servers hold a random, server-specific gateway credential, not Relay’s Firebase key. The official app proves receipt of a challenge directly to `https://push.r3l4y.dev`. A server can send only to devices enrolled for its origin. Server switches move that authorization; logout revokes it. Existing app builds without the gateway enrollment update cannot use this flow.

Localhost-only deployments work in the browser; background Android push requires a public HTTPS origin, the updated official app and gateway availability. The gateway sees notification payloads and device tokens when forwarding them. It has rate limits and a single-instance availability boundary; it is not an end-to-end encrypted notification transport or delivery guarantee.

## Capacity and costs

Home lab is one machine with no high availability. Production EC2 profiles support configurations for 10, 100 and 1,000 **registered** users. These are sizing models, not measured concurrent-call guarantees. Larger EKS modules are retained, but the installer rejects 10k+ until an immutable release is certified for distributed operation.

AWS pricing is an estimate, not a spending limit. The estimator fails on unclassified billable resource types. Review modeled traffic, backups, exclusions and your account’s budgets. No customer infrastructure is created by this repository’s CI.

## Maintenance

See [operations and recovery](docs/operations.md), [security boundaries](docs/security.md), and [upstream extraction](UPSTREAM.md). Images and tools are pinned by digest. Back up `.relay/` securely: Terraform state and plan files are private operational data and must never be committed.
