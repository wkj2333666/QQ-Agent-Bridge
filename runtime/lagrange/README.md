# Lagrange.OneBot Setup (local)

This is the recommended gateway for arm64/RPi.

## Bring up order (per plan)

1. Start the python bridge first (with --echo-only) so WS is listening.
2. docker compose up -d
3. docker logs -f lagrange   (watch for QR, scan with QQ app)
4. After login, edit ../../config.yaml :
   - onebot.access_token must match this appsettings
   - bot.self_id = your logged QQ number
5. Restart bridge without --echo-only
6. Test from your QQ private chat.

## Effective config

The Docker image runs with working directory `/app/data`, so the effective config is:

```text
runtime/lagrange/data/appsettings.json
```

The top-level `runtime/lagrange/appsettings.json` is kept as a template/reference unless the compose file is changed to mount it into `/app/data/appsettings.json`.

## Edit token

Change "AccessToken" in `data/appsettings.json` and in config.yaml to same strong value.
Never commit the real token.

## Lagrange signer failures

If logs show `Signer server returned a NotFound` and `All login failed!`, first check:

- `data/appsettings.json` is the file being edited.
- `SignServerUrl`, `SignProxyUrl`, and `MusicSignServerUrl` are not stale public signer URLs.
- The bridge URL/port matches the reverse WebSocket listener.

If it still fails after config is confirmed, consider switching to NapCatQQ instead of repeatedly restarting Lagrange.

## Rollback

docker compose down
rm -rf data/

## Notes

- Uses host network so 127.0.0.1 from inside container reaches host bridge.
- If issues, try adding --network=host explicitly or use your LAN IP in appsettings.
- Update image: docker pull ghcr.io/lagrangedev/lagrange.onebot:edge
