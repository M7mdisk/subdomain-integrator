# Subdomain integrator operator

`subdomain-integrator` exposes many related workloads through a single Gateway address.

For each related application on the `ingress` relation, it derives the hostname:

`<normalized-app-name>.<base-domain>`

Example with `base-domain=demos.canonical.com` and app `app-test1`:

`app-test1.demos.canonical.com`

## Deploy

```bash
juju deploy ./subdomain-integrator.charm \
  --config gateway-class=<gateway-class> \
  --config base-domain=demos.canonical.com
```

## Relate workloads

```bash
juju relate subdomain-integrator:ingress app-test1:ingress
juju relate subdomain-integrator:ingress app-test2:ingress
```

All related apps share one gateway address reported on the unit status.

## Expose through HAProxy

To expose those hostnames through an existing HAProxy provider, relate:

```bash
juju integrate subdomain-integrator:haproxy-route <haproxy-provider>:haproxy-route
```

The charm publishes a wildcard route for `*.${base-domain}` to the current Gateway API address.
This route stays the same as applications join or leave; individual application hostnames are
never sent to HAProxy for certificate issuance. The Gateway API routes each application hostname
to its workload.

Configure a single wildcard DNS record, for example
`*.marketplace.demos.canonical.com`, pointing to the HAProxy ingress. The optional `dns-record`
relation also requests a single wildcard record, pointing directly to the Gateway address.
Individual application DNS records are not required.

When terminating TLS at the Gateway with `enforce-https=true` and a `certificates` relation,
the charm requests one certificate for `*.${base-domain}`, not individual application certificates.
Kubernetes Secret names continue to use the base domain without the wildcard.

Existing per-application ACME placeholder records are not removed by this change. They can prevent
wildcard DNS synthesis for those application names and must be reviewed separately when migrating
from individual certificates. ACME DNS-01 TXT records are still needed to validate the wildcard
certificate, at `_acme-challenge.${base-domain}`.
