# Local Data Lake and analysis services

The Windows crawler writes JSON into `data/raw`. Storage then publishes a raw snapshot to MinIO. The app restores the snapshot, checks SHA256, runs cleaning, and writes JSONL plus SQLite under `data/processed/runs/<run_id>`. SQLite is an embedded database on the mounted folder; there is no separate DB server container.

Copy `.env.example` to `.env`, choose private MinIO credentials (username at least 3 characters, password at least 8), and fill `RSTUDIO_PASSWORD` if using RStudio. The Python CLI reads process environment, **not** `.env`; Compose loads `.env` for container values. The supplied services publish ports on localhost for local coursework.

```powershell
docker compose up -d --build minio
docker compose run --rm --build app python -m src.storage.lake upload
docker compose run --rm app
```

The upload command prints the full raw `snapshot_id`. Use it to reproduce a particular input instead of `latest`:

```powershell
docker compose run --rm app python -m src.storage.lake process --snapshot-id <64-character-id> --config configs/preprocessing.json
```

Run `docker compose up --build` after the initial upload to start MinIO and process its latest completed snapshot. App is a batch job and exits when processing finishes. A missing snapshot is a visible error; it never silently falls back to local raw data. To import checked human labels, append `--annotations data/annotations/reviews.jsonl` to the processing command.

Open MinIO's console at `http://localhost:9001`. Inspect the private `ady-raw` bucket: `raw/blobs`, `raw/snapshots/<id>/manifest.json`, and `raw/latest.json`. The manifest is written only after all files upload, and the latest pointer only after the manifest. Orphan blobs from interrupted uploads can remain; they are ignored. Input files are read once and validated; snapshots preserve per-file consistency, so stop the crawler first if a coordinated cross-source capture is needed.

The upload filter includes Foody datasets and Maps final/partial review lists. It excludes browser profiles, diagnostics, queues, and status/place metadata. The adapter validates JSON list shape; row-level validation remains the cleaner's responsibility. Restoring verifies checksums and never merges stale cache files into another snapshot. Rerunning with unchanged inputs resolves to the same snapshot ID. Use one publisher at a time when relying on `latest` (last successful publisher wins).

Optional RStudio:

```powershell
docker compose --profile analysis up -d --build rstudio
```

Open `http://localhost:8787`, user `rstudio`, password from `.env`. Processed datasets are mounted read only at `/home/rstudio/project/data/processed`. Read `latest.json`'s `run_id`, then construct `runs/<run_id>/reviews.sqlite` within that mount; absolute paths in the pointer can refer to a different host/container. Notebooks are writable under `/home/rstudio/project/notebooks`.

The app image installs storage/ML packages only; Chrome and Selenium crawling run on Windows. `requirements.txt` installs the host packages. Python 3.11 is the documented image/runtime target. The dependency files use major-version bounds, not a tested lock. After a successful full environment test, record `pip freeze` and image digests for reproducible delivery.

MinIO community distribution is source-only and its repository is archived. This coursework Dockerfile builds the verified `RELEASE.2025-10-15T17-29-55Z` source release instead of relying on a moving prebuilt image. Review an actively supported deployment option before external hosting. References: [official MinIO repository](https://github.com/minio/minio), [specified release](https://github.com/minio/minio/releases/tag/RELEASE.2025-10-15T17-29-55Z), [Python SDK API](https://github.com/minio/minio-py/blob/master/docs/API.md), [Compose profiles](https://docs.docker.com/compose/how-tos/profiles/), [Rocker RStudio](https://rocker-project.org/images/versioned/rstudio.html).

Validation in this environment: offline storage tests exercise publication ordering, hash verification, unsafe paths, cache tampering and MinIO-to-SQLite processing. Docker CLI is unavailable here, so image builds, service health and live credentials/connectivity still require a local integration run.
