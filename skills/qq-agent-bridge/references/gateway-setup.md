# OneBot Gateway Setup

Use this reference when the machine does not already have a QQ gateway that exposes OneBot v11 events and actions.

## Core Distinction

OneBot v11 is a protocol, not the thing to install. Install and configure one QQ gateway that implements OneBot v11, then point it at the local bridge.

Common choices:

| Gateway | Use when |
|---|---|
| NapCatQQ | You want the default personal QQ gateway path with active OneBot support |
| Lagrange | You explicitly need this gateway and accept signer/protocol fragility |
| LLOneBot | You already run QQ/NTQQ with this OneBot adapter |
| QQ Open Platform | You need official compliance more than personal-account flexibility |

Do not invent install commands from memory. For the selected gateway, read its current official installation docs or local README before running package, Docker, or login commands.

## Environment Checklist

Before implementation, collect:

- OS and deployment shape: local desktop, server, Docker, systemd, or existing QQ client.
- Selected gateway and version.
- OneBot mode: prefer reverse WebSocket for a local bridge.
- Bridge URL, for example `ws://127.0.0.1:8765/onebot`.
- Access token shared between gateway and bridge.
- Bot QQ account/self id after login.
- Allowed `user_id`, `group_id`, command prefix, and workspace roots.

## Safe Install Policy

Default to a reversible, isolated install:

- Prefer Docker Compose or Podman Compose with all files under the current project or an explicitly approved runtime directory.
- Do not run `sudo`, global package managers, `curl | bash`, systemd installation, shell installers, or force reinstall flags unless the user explicitly approves the exact command.
- Do not install Docker/Podman for the user. If no container runtime exists, stop and ask.
- Bind WebUI and OneBot ports to localhost when possible.
- Use `restart: "no"` during initial bring-up so login or signer failures do not loop endlessly.
- If the bridge listens on `127.0.0.1`, host networking can be safer than rebinding the bridge to `0.0.0.0`; document any WebUI port exposure before starting.
- Map container uid/gid to the current user when the gateway image supports it, preferably through the image's documented environment variables.
- Do not add Compose `user:` when the image entrypoint creates or switches to an internal user; that can break startup with `useradd: Permission denied`.
- Mount only gateway data/config directories, never the whole home directory or project workspace.
- Before running commands, show the exact commands, created paths, opened ports, and rollback commands.

Use this project-local shape when the user wants the safest default:

```text
runtime/
  napcat/
    compose.yml
    data/
    config/
```

The only expected host changes are Docker images/containers/networks and files under that runtime directory.

## Reverse WebSocket Configuration

Configure the gateway so it connects to the bridge. In NapCat WebUI this means **网络配置 -> Websocket客户端**, not **Websocket服务器**:

```yaml
onebot:
  mode: "reverse-websocket"
  url: "ws://127.0.0.1:8765/onebot"
  access_token: "change-this-token"
```

Names and file locations differ by gateway. Preserve the selected gateway's native config format; the YAML above is only the contract the bridge needs.

If a NapCat **Websocket服务器** entry is listening on the bridge port, disable it. The bridge owns the WebSocket server port; NapCat should initiate the outbound client connection.

## Bring-Up Order

1. Install or start the selected QQ gateway from its current official docs.
2. Log in with a test QQ account and confirm the gateway receives message events.
3. Start the local bridge with an echo-only handler.
4. Configure reverse WebSocket URL and access token on both sides.
5. Send a private test message and verify the bridge sees a OneBot event.
6. Send an allowed group message with bot mention plus prefix and verify echo reply.
7. Only after echo works, enable the agent adapter.

## Hard Stops

- Do not store QQ credentials, QR login data, cookies, or tokens in the repo.
- Do not expose the bridge WebSocket publicly unless there is a separate authenticated reverse proxy and explicit reason.
- Do not enable group chat before allowlists and prefix/mention checks are configured.
- Do not debug by dumping raw private chat logs into QQ replies or committed files.

## Lagrange Signer Failures

If Lagrange exits with `Signer server returned a NotFound` or `All login failed!`, first inspect the mounted `appsettings.json`:

- A stale `SignServerUrl` can force all login traffic through a broken external signer.
- Empty `SignServerUrl`, `SignProxyUrl`, and `MusicSignServerUrl` are safer than hardcoding an old public signer from memory.
- Do not repeatedly restart the container without changing evidence; that only replays the same failed login path.

If removing a stale signer still fails, stop and consider switching the gateway to NapCatQQ before spending time on protocol-signing workarounds.
