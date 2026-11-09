# Isolated Linux acceptance

This directory is executable test infrastructure, not a production compose override.
Nothing here has been started on the production daemon. Local Windows has no WSL/Linux Docker.

## Explicit test-host launch

Copy only the reviewed checkout to a host approved for isolated Docker tests. Do not copy .env, .git credentials, uploads, backup dumps or private evidence. Base compose has no host ports, bind mounts, Docker socket, host network or production endpoint. It creates separate named test volumes and an internal-only network. The script generates an ephemeral hexadecimal PostgreSQL password in memory; it never writes .env.

From that checkout on the approved Linux host:

    PROJECT1_TEST_HOST_APPROVED=1 \
    PROJECT1_PROTECTED_HTTP_URLS="<approved-read-only-health-URLs>" \
    sh testing/run-linux.sh

This command builds only test images, starts only project1-rc-acceptance-<UTC-runid>, runs clean Alembic migration, checks real PostgreSQL15/Redis7, tests common SMTPTransport against the local non-relaying sink, exercises Redis reserve/process-loss/restart/redelivery/ACK, and runs pytest tests -q. Only that project's redis service is restarted. The runner has no Docker socket. After test or abort, only the freshly created labelled test namespace is stopped and removed, including its volumes/networks/images. New base-image tags are removed only if absent before the run; no force removal or global prune is used. Before/after snapshots protect all pre-existing containers. The approval variable is an execution guard, not a substitute for user authorization to use a shared daemon.

Runtime caps: PostgreSQL512MiB/0.5CPU, Redis128MiB/0.25CPU, SMTP256MiB/0.25CPU, runner1536MiB/1CPU; total2432MiB and2CPU. Bounded json-file logs10MiB×3 per service. Legacy Docker builds run sequentially before services, each with --memory=1g --memory-swap=1g --cpu-period=100000 --cpu-quota=100000. There is no fallback to unbounded BuildKit. Shared daemon/image-pull overhead is monitored by watchdog, not covered by per-container cgroup caps. Registry/PyPI access is needed during build, not from running internal-network containers.

## Explicit PDF corpus opt-in

Base tests do not read host data. To run the separately approved193-PDF regression, copy only verified fixtures and the capture JSON to the test host, then opt in to the additional compose file through the script:

    PROJECT1_TEST_HOST_APPROVED=1 \
    PROJECT1_TEST_PDF_FIXTURES=1 \
    PDF_FIXTURE_DIR=/private-test-inputs/pdf-fixtures \
    PDF_CAPTURE_FILE=/private-test-inputs/pdf-corpus.json \
    sh testing/run-linux.sh

Only those two paths are mounted read-only into /fixtures/pdf and /fixtures/pdf-corpus.json. No parent directory or backup archive is mounted. On the authoring PC the reviewed copies are ../implementation-evidence/pdf-fixtures and ../implementation-evidence/pdf-corpus.json; these private inputs are not copied into images or committed. Missing corpus is NOT RUN, never full PDF acceptance.

## Evidence and limits

Save stdout/test artifacts outside source control. The sink stores synthetic MIME plus SHA256 in its private named volume and never relays. It rejects non-synthetic sender/recipient domains. Actual production provider delivery/inbox placement is outside this test.

Linux Docker/Redis transport/prefork tests remain NOT RUN until this exact approved isolated test-host launch completes. A local Windows PostgreSQL run or Celery memory broker does not satisfy that gate. The standalone transport probe complements, not replaces, application Celery prefork/process-loss tests.

## Protected-host watchdog

The launcher requires healthy baseline read-only HTTP URLs supplied by the operator, at least4CPUs,8GiB available RAM,20GiB Docker storage and load<=half the CPU count. Every5s it checks unchanged pre-existing IDs/start times/status/health/restart count/OOM, protected HTTP code/latency, RAM headroom, disk, sustained host load and CPU pressure. Degradation cancels only this run and cleans only its labelled namespace. The protected services are never restarted, reconfigured or pruned. All test images carry a unique run label before their first build RUN. Unsupported bounded legacy builder is a hard failure, never an unbounded fallback.
