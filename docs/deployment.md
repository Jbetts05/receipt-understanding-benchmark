# Deploy Receipt Bench

The recommended deployment path is one guided PowerShell command built around
Azure Developer CLI and Bicep.

## Prerequisites

- Azure CLI authenticated with `az login`.
- Azure Developer CLI 1.27 or later authenticated with `azd auth login`.
- PowerShell 7.
- Python 3.12.
- Permission to create resource groups, Azure AI resources, deployments, role
  assignments, networking, Storage, Key Vault, monitoring, and App Service.
- Subscription-scope permission
  `Microsoft.Resources/subscriptions/resourceProviders/register/action`, or a
  subscription administrator who has already registered the required resource
  providers.

## Check availability without deploying

```powershell
pwsh ./scripts/deploy.ps1 `
  -EnvironmentName receipt-bench-yourname `
  -ValidateOnly
```

This checks the selected subscription, regional model catalog, and quota. It
does not create Azure resources.

## Deploy

```powershell
pwsh ./scripts/deploy.ps1 -EnvironmentName receipt-bench-yourname
```

The script:

1. Uses the Azure CLI's current subscription unless `-SubscriptionId` is set.
2. Creates or selects an `azd` environment.
3. Derives a unique Foundry account name and `rg-<environment>` resource group.
4. Registers required Azure resource providers, or stops with the exact
   subscription-level administrator action when registration is not permitted.
5. Validates the complete model catalog and quota before creating resources.
6. Creates or verifies the Foundry account and project.
7. Deploys the models in `configs/azure-deployments.json` at minimum capacity.
8. Grants the deployment operator **Cognitive Services User** when required,
   waits for RBAC propagation, and configures the SROIE and CORD Content
   Understanding analyzers using the operator's Microsoft Entra identity.
9. Stores non-secret endpoint values in ignored local and `azd` environment files.
10. Runs `azd provision --preview` and waits for explicit confirmation before
   `azd up`.
11. Provisions App Service, private Storage and Key Vault networking, monitoring,
    managed identities, and least-privilege data roles, including Cognitive
    Services User on the Foundry account.
12. Packages and deploys the application.

Use `-Yes` to accept the preview non-interactively in an automation context.

## Authentication

Hosted deployments use Microsoft Entra ID by default:

```powershell
pwsh ./scripts/deploy.ps1 `
  -EnvironmentName receipt-bench-yourname `
  -AuthMode entra
```

The App Service system identity receives **Cognitive Services User** on the
Foundry account. This works when Azure Policy sets `disableLocalAuth=true`; no
Foundry account key is requested or stored. If an earlier key-mode deployment
stored a key, switching to Entra mode removes that secret; switching back safely
recovers the soft-deleted secret name before writing a fresh value.

`-AuthMode auto` is the default. It tries Entra authentication first while
configuring analyzers and can fall back to an account key only when local
authentication is enabled. `-AuthMode key` remains available for an existing
permissive environment, but fails clearly when policy disables key access.

Local developers use `DefaultAzureCredential` in Entra mode, so run `az login`
with an identity that has **Cognitive Services User** on the Foundry resource.
The hosted app sets `AZURE_AI_CREDENTIAL_KIND=managed_identity` and uses
`ManagedIdentityCredential` directly.

## Provider registration boundary

Resource-provider registration is separate from Foundry inference
authentication. If registration fails for a namespace such as
`Microsoft.Insights`, ask a subscription Owner or Contributor (or a custom role
holder) to register the namespace or grant:

```text
Microsoft.Resources/subscriptions/resourceProviders/register/action
```

The assignment must apply at subscription scope. Resource-group Contributor
does not grant subscription provider registration. Rerun the same deployment
after the provider state is `Registered`.

## Reuse an existing Foundry account

The account and project can be reused from another resource group:

```powershell
pwsh ./scripts/deploy.ps1 `
  -EnvironmentName receipt-bench-yourname `
  -SubscriptionId <subscription-id> `
  -Location swedencentral `
  -ResourceGroup <new-application-resource-group> `
  -FoundryResourceGroup <existing-foundry-resource-group> `
  -FoundryAccount <account-name> `
  -FoundryProject <project-name>
```

Compatible existing model deployments are retained. A deployment with the same
name but a different model, version, format, or SKU is rejected rather than
overwritten. The guided script reads the existing account's immutable
`customSubDomainName` and uses it for Foundry, OpenAI, and Content Understanding
endpoints; it does not assume the ARM account name is also the endpoint host.

## Model and quota boundary

The product exposes 22 comparison rows backed by ten model deployments and
several native Azure AI services. A fresh subscription might not have access or
quota for every preview or Marketplace model. The deployment intentionally
fails with the unavailable model or quota name instead of presenting a
partially configured comparison as ready.

To use a different supported model set, update these files together:

- `configs/azure-deployments.json`
- `configs/solutions.yaml`
- `src/receipt_bench/web/catalog.py`
- `configs/pricing.yaml`

Then run the complete test and package validation suite.

## SROIE rights

Hosted SROIE seeding is disabled by default. Enable it only when you hold
independent redistribution authorization:

```powershell
pwsh ./scripts/deploy.ps1 `
  -EnvironmentName receipt-bench-yourname `
  -SroieRightsEvidence "<your evidence reference>"
```

CORD and user-imported datasets do not depend on this switch.
On a redeployment, omitting `-SroieRightsEvidence` preserves the current `azd`
environment value. Pass an explicit empty string only when you intend to disable
hosted SROIE access.

## Direct `azd` usage

After the guided script has populated an environment, normal application or
infrastructure iterations can use:

```powershell
azd provision --preview
azd up
```

Do not copy another operator's `.env` or `.azure/<environment>` directory.
Those files can contain provider credentials and are intentionally ignored.
In the default Entra deployment they contain endpoints and authentication mode,
not a Foundry API key.
