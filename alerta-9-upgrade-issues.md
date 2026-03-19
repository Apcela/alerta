# Alerta 9.x Upgrade Issues

Issues encountered while upgrading from Alerta 5.2.9 to 9.0.4.

## Issue 1: Tags Query Parameter Causes 500 Error (PyMongo 4.x)

### Summary
Any API request using the `tags` query parameter returns a 500 Internal Server Error with PyMongo 4.x.

### Steps to Reproduce
```bash
# This works:
curl "https://alerta.example.com/api/alerts?status=open"

# This fails with 500:
curl "https://alerta.example.com/api/alerts?tags=any-tag-value"
```

### Error Message
```json
{
  "code": 500,
  "errors": ["documents must have only string keys, key was None"],
  "message": "An internal error has occurred!",
  "status": "error"
}
```

### Stack Trace (from logs)
```
bson.errors.InvalidDocument: documents must have only string keys, key was None
```

### Analysis
- The error occurs immediately on fresh database with no legacy data
- Only the `tags` query parameter triggers this - other queries work fine
- The UI loads successfully (doesn't use tags parameter in the same way)
- This appears to be a bug in alerta's tag query building code that creates a query document with `None` as a key, which PyMongo 4.x rejects (PyMongo 3.x was more permissive)

### Impact
- Any integration querying alerts by tag fails
- Escalation services, monitoring scripts, etc. that filter by tags are broken

### Environment
- Alerta 9.0.4
- alerta/alerta-web:9.0.4 Docker image
- PyMongo 4.x (bundled with image)
- MongoDB 4.4+

---

## Issue 2: PyMongo 4.x + uWSGI Prefork Incompatibility

### Summary
The default alerta-web Docker image uses uWSGI in prefork mode (master process forks workers). PyMongo 4.x explicitly does not support creating a MongoClient before fork and using it in child processes.

### Symptoms
- API requests hang/timeout
- Workers become unresponsive
- Intermittent 504 Gateway Timeout errors

### Root Cause
The default uWSGI configuration:
1. Master process loads the Flask app (creating MongoClient)
2. Master forks worker processes
3. Workers inherit the MongoClient, which is in an invalid state

PyMongo 4.x documentation explicitly warns against this pattern.

### Workaround
Add `lazy-apps = true` to uwsgi.ini, which causes each worker to load the app independently after fork:

```ini
[uwsgi]
lazy-apps = true
```

### Caveat
With `lazy-apps = true`, each worker runs full app initialization independently, including database index creation. This means:
- Index drop/create operations run N times (once per worker)
- This is noisy but safe (index operations are idempotent)
- For single-worker deployments, this is not an issue

### Recommendation for alerta-web image
Consider adding `lazy-apps = true` to the default uwsgi.ini template, or documenting this requirement for PyMongo 4.x compatibility.

---

## Issue 3: UWSGI_MAX_WORKER_LIFETIME Environment Variable

### Summary
The base alerta-web:9.0.4 image sets `UWSGI_MAX_WORKER_LIFETIME=30` as an environment variable, causing workers to be killed and respawned every 30 seconds.

### Problem
With `lazy-apps = true` (required for PyMongo 4.x), each worker respawn triggers full app initialization including MongoDB index operations. Combined with the 30-second lifetime:
- Constant index churn in logs
- Brief service interruptions during worker respawn (especially with single worker)

### Additional Issue
uWSGI environment variables take precedence over config file settings. Setting `max-worker-lifetime = 0` in uwsgi.ini has no effect if `UWSGI_MAX_WORKER_LIFETIME=30` is set in the environment.

Source confirmation: uWSGI checks `uwsgi.max_worker_lifetime > 0` to enable the feature, so `0` disables it.

### Workaround
Override the environment variable in your Dockerfile or runtime:
```dockerfile
ENV UWSGI_MAX_WORKER_LIFETIME=0
```

Or for single-worker deployment (simpler, avoids fork issues entirely):
```dockerfile
ENV UWSGI_PROCESSES=1
ENV UWSGI_MAX_WORKER_LIFETIME=0
```

---

## Issue 4: Multi-Worker Scalability Limitations

### Summary
Alerta's app initialization runs `_create_indexes()` which drops and recreates database indexes. This runs on every app initialization.

### Impact
- With `lazy-apps = true`: each worker runs index operations on startup
- Multiple containers: all containers race to drop/recreate indexes on startup
- Index operations are idempotent (no data loss), but wasteful

### Note
`drop_indexes()` only removes index structures, not document data. This is safe but inefficient for multi-worker/multi-container deployments.

### Recommendation
Consider making index creation a separate migration step or adding a check to skip if indexes already exist.

---

## Working Configuration

For running alerta 9.0.4 with PyMongo 4.x, we use:

**Dockerfile:**
```dockerfile
ARG ALERTA_VERSION="9.0.4"
FROM alerta/alerta-web:${ALERTA_VERSION}

# Override base image uwsgi settings
ENV UWSGI_PROCESSES=1
ENV UWSGI_MAX_WORKER_LIFETIME=0

# Copy custom uwsgi config with lazy-apps=true
COPY uwsgi.ini /app/uwsgi.ini
```

**uwsgi.ini:**
```ini
[uwsgi]
master = true
processes = 1
listen = 100

; Fix for PyMongo 4.x - each worker must initialize app independently
lazy-apps = true

; App configuration
module = wsgi
callable = app
manage-script-name = true
mount = /api=wsgi:app

; Logging
log-date = %%Y-%%m-%%dT%%H:%%M:%%S
logformat-strftime = true
logformat = %(ftime) alerta[%(pid)]: [%(proto) %(status)] %(method) %(uri) (%(rsize) bytes, %(msecs) ms) request_id=%(var.X-Request-ID) ip=%(addr)
```

---

## Environment Details

- Upgraded from: Alerta 5.2.9 (PyMongo 3.7.1)
- Upgraded to: Alerta 9.0.4 (PyMongo 4.x)
- Deployment: AWS ECS Fargate
- MongoDB: 4.4+ (also on Fargate, ephemeral storage)
