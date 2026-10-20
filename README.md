# Dobyvatel Revival

Community-made server emulator for **Dobyvatel** (THX Games Zrt., 2019), developed to bring back private-server play after the original online services became unavailable.

This is an independent fan revival, not affiliated with or endorsed by the original game's publisher.

## Current features

- Python HTTP backend for login, accounts, sessions and village progression
- Trivia and match flow with bot opponents (work in progress)
- Persistent player accounts and progression
- Custom Android server selection (choose the default server or enter a host and port)
- Server administration using `server/manage.py`
- Integration metadata for [Server Manager](https://github.com/AriCuteGirl/Server-Manager-GUI) to host on a PC or VPS

## Hosting your own server

**Requirements:** Python 3.10 or newer and a PC or VPS with a reachable TCP port. The default port used in this example is `18080`.

```bash
git clone https://github.com/AriCuteGirl/Dobyvatel-Revival.git
cd Dobyvatel-Revival
DOBYVATEL_PUBLIC_HOST=YOUR_REACHABLE_IP \
DOBYVATEL_PUBLIC_PORT=18080 \
python3 -u server/dobyvatel_server.py --host 0.0.0.0 --port 18080
```

Use your PC's LAN IP if everyone is on the same Wi-Fi; use a public IP/domain and appropriate firewall/network configuration for remote connections. The `DOBYVATEL_PUBLIC_HOST` value must be reachable from your Android phone. Don't set it to an internal emulator/Waydroid address unless the phone can actually reach that address.

Test the endpoint:

```bash
curl http://127.0.0.1:18080/health
```

Server files and supporting management utilities are being prepared for the first source release; if the command above refers to files not yet present, wait for the source upload.

## Android client

The **Dobyvatel Revival** Android build includes an in-app server selector. To join a self-hosted instance, choose the custom server option and enter `HOST:PORT` (for example `192.168.1.20:18080`). Rebuilding an APK for each server address is not required when using the server-selector build.

**APK release status:** A public APK download has not been uploaded yet. Android packages and original game data have separate redistribution considerations; the original game's assets are not included in the server-source repository.

## Server Manager

[Server Manager](https://github.com/AriCuteGirl/Server-Manager-GUI) helps install and manage servers on Linux/Windows PCs and SSH-accessible VPS hosts. Its source repository is private; the public repository distributes the desktop application.

## Security and privacy

Do not commit or upload private SSH keys, Oracle deployment settings, player databases, generated logs, signing certificates, or session tokens. Keep persistent account data out of Git.

## Project status

This is an experimental fan revival, not a complete recreation of every original game mode. Report problems through GitHub Issues.

---
Original Dobyvatel and associated assets belong to their respective rights holders.
