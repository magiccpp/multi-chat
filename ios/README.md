# MultiChat for iPhone

A native SwiftUI client (iOS 17+, so the iPhone 13 and newer) for the same MultiChat server. All sessions and chats live on the server. The app shows the same history as the desktop app and the TUI.

**What it does**
- Sessions list (server history). Start a session, pick models from any endpoint, choose shared or isolated.
- Chat with one tab per model, live streaming replies, tool calls and their output, ▲/▼ votes and **Pick as builder**.
- Switch PLAN/BUILD, change the builder, approvals (edits / commands: auto or ask) and the models in the session.
- **Approvals**: approve or reject commands, and review edits as a colored diff, while a runner (desktop app, TUI or `multichat runner`) or an SSH host does the work.
- Attach a folder on a runner or SSH host. The phone itself never runs commands.
- Per-model token and $ meter for the session; statistics with Swift Charts; a read-only view of the server's endpoints, models and prices.

**What it doesn't do**: run commands or edit files on the phone, or edit endpoints and keys. Those stay on the server and the desktop app.

## Build and install on your iPhone (needs a Mac)

1. Install **Xcode 16 or newer** from the App Store, plus XcodeGen: `brew install xcodegen`.
2. Generate the project and open it:
   ```bash
   cd ios
   xcodegen            # creates MultiChat.xcodeproj from project.yml
   open MultiChat.xcodeproj
   ```
3. In Xcode, select the **MultiChat** target, then **Signing & Capabilities**:
   - **Team**: your Apple ID (Xcode → Settings → Accounts → add it). A free Apple ID works for your own phone; the app then has to be re-installed every 7 days. A paid developer account removes that limit and enables TestFlight.
   - **Bundle Identifier**: change `com.example.multichat` to something unique, e.g. `com.yourname.multichat`.
4. Connect the iPhone with a cable, or over Wi-Fi once paired. On the phone, enable **Settings → Privacy & Security → Developer Mode** (it restarts). Pick the phone as the run destination and press **Run** (⌘R).
5. The first launch may say "Untrusted Developer". Allow it in **Settings → General → VPN & Device Management**.
6. In the app, enter the server URL and the token (`MULTICHAT_API_TOKEN` from the server's `.env`). The token is stored in the Keychain.

## The server must be reachable over HTTPS

iOS only allows plain `http://` to local-network addresses (`.local` names and LAN IPs, for testing at home). For use anywhere:

```bash
# on the server: point a DNS name at it, open ports 80 and 443, then
echo "MULTICHAT_DOMAIN=multichat.example.com" >> .env
docker compose -f compose.yaml -f compose.https.yaml up -d     # or nerdctl compose …
```
Caddy obtains a Let's Encrypt certificate automatically. Use `https://multichat.example.com` in the app (and in the desktop app and TUI). Tailscale with MagicDNS HTTPS also works.

## Code layout and tests

```
ios/
  project.yml            XcodeGen spec (iOS 17, iPhone)
  MultiChat/             SwiftUI app: AppModel, SessionModel, Views/
  MultiChatKit/          Swift package: API client, SSE parsing, models, session state (no UI)
```
`MultiChatKit` is plain Swift and also builds on Linux. Its tests run against JSON fixtures, and against a live server when `MULTICHAT_TEST_URL` and `MULTICHAT_TEST_TOKEN` are set:

```bash
cd ios/MultiChatKit && swift test
# Linux without Swift installed:
docker run --rm --network host -v $PWD:/pkg -w /pkg swift:6.0-jammy swift test
```
CI (`.github/workflows/ios.yml`) runs those tests on Linux and macOS and builds the app for the iOS simulator.
