Quick start — build and run with Docker

1) Build images with docker-compose

```bash
docker compose build
docker compose up -d
```

2) Open the frontend at http://localhost:3000 and the backend API at http://localhost:8080/api

3) Place additional scripts in the `scripts/` folder and add them via the UI or the API using the container path (e.g. `/app/scripts/backup-daily.sh`).

4) To view live backend logs:

```bash
docker compose logs -f backend
```
