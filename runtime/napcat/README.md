# NapCatQQ Setup

This runtime keeps NapCat isolated under `runtime/napcat/`. It does not install system packages, create systemd services, or modify the Lagrange runtime.

## Why Host Network

The bridge currently listens on:

```text
ws://127.0.0.1:38765/onebot
```

NapCat needs to connect back to that reverse WebSocket URL. `network_mode: host` lets the container reach the host bridge at `127.0.0.1` without changing the bridge bind address to `0.0.0.0`.

During initial bring-up, `restart: "no"` is intentional so login or config failures do not loop endlessly.

## Start

From the repository root:

```bash
cd runtime/napcat
mkdir -p data/qq config plugins
BRIDGE_WORKSPACE_ABS="$(cd ../../workspace && pwd)" \
  NAPCAT_UID=$(id -u) NAPCAT_GID=$(id -g) docker compose up
```

Set `agent.default_workspace` in `config.yaml` to the same absolute path as
`BRIDGE_WORKSPACE_ABS`. NapCat needs the path inside the container to match the
path used by the bridge for `file://` uploads produced by `/task` and `/code`.

Then open:

```text
http://127.0.0.1:6099/webui
```

NapCat Docker's default WebUI token is commonly `napcat`; change it before long-running use.

## Configure OneBot

In NapCat WebUI, open **网络配置 -> Websocket客户端**, not **Websocket服务器**.

Create a WebSocket client that points to the bridge:

```text
ws://127.0.0.1:38765/onebot
```

Use the same access token as `config.yaml`:

```yaml
onebot:
  port: 38765
  path: "/onebot"
  access_token: "<same value>"
```

If you already created a **Websocket服务器** entry on port `38765`, disable or delete it. In this architecture the Python bridge is the WebSocket server; NapCat is the WebSocket client.

Start the bridge first in echo mode:

```bash
cd <repo>
. .venv/bin/activate
python -m src.qq_agent_bridge.main --echo-only
```

After QQ login, fill `bot.self_id` in `config.yaml`, then run the bridge normally.

## Rollback

```bash
cd <repo>/runtime/napcat
docker compose down
```

To remove only NapCat local state later, delete `runtime/napcat/data/`, `runtime/napcat/config/`, and `runtime/napcat/plugins/`.

## Safety Notes

- Do not commit files under `data/`, `config/`, or `plugins/`.
- Do not paste QQ credentials, QR data, cookies, or tokens into tracked files.
- Keep Lagrange stopped while testing NapCat so only one gateway connects to the bridge.
