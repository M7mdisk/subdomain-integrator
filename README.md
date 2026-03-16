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
