# Local Docker deployment

Run these commands from the repository root. The existing native Python
service on port 8877 must be stopped first.

```powershell
docker compose --env-file .env -f deploy/local/compose.yaml up -d --build
docker compose --env-file .env -f deploy/local/compose.yaml ps
docker compose --env-file .env -f deploy/local/compose.yaml logs --tail 100 story-service
```

Open `http://127.0.0.1:8877/`. This local compose file binds to loopback by
default and uses development review mode. The `approved/`, `drafts/`,
`exports/` and `service/.runtime/` directories remain on the host; no family
media is copied into the image. The `drafts/` mount is required for the
workbench review queue. To open it to a trusted LAN, set `RORO_LISTEN_ADDRESS`
to the selected host address and restrict access with the Windows firewall script.

In an administrator PowerShell, allow only the trusted local subnet:

```powershell
.\service\enable-lan-firewall.ps1 -Port 8877
```

Stop it with:

```powershell
docker compose --env-file .env -f deploy/local/compose.yaml down
```
