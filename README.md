# 🧬 Ribo-Rumble

A real-time multiplayer LAN-party game where players are simultaneously
biochemists translating each other's "DNA" messages into polypeptide
"passwords". DNA and translations flow entirely through the app while it
handles state, scoring, and the silently lurking final reckoning.

## How the game plays

Each player is both a **requester** and a **decrypter** at all times.

- As a requester: invent a protein name and a DNA sequence and log them
  in the app. The app randomly assigns another player/team to decrypt it.
- As a decrypter: open received requests in the app, read the DNA there,
  work out the polypeptide using the codon table, and submit your guess
  in the app.
- The requester then **confirms** or **rejects** the decryption.

### Scoring

During the game:

| Requester's call | DNA | Submission | Decrypter | Requester |
|---|---|---|---|---|
| Confirm | valid | correct | **+3** | 0 |
| Confirm | valid | wrong | **+3** | **−2** |
| Confirm | invalid | (any) | **+3** | **−2** |
| Reject | valid | correct | **−3** | **−2** |
| Reject | valid | wrong | **−3** | 0 |
| Reject | invalid | (any) | 0 | **−3** |

At game end (sweep):

| Pending state | Effect |
|---|---|
| Request never decrypted | Decrypter **−2** |
| Submission never resolved | Decrypter **+2** |

Two key consequences:
- A wrongly-decrypted answer is worth **+2** if the requester forgets to
  reject in time. Sit on bad guesses; the clock is your friend.
- Conversely, the requester is *also* on the clock: forgetting to confirm
  a correct answer leaks +2 to the decrypter you wanted to deny.
- The requester is graded too. Confirming a wrong answer or rejecting a
  correct one costs the requester **−2** on top of whatever happens to
  the decrypter. Pay attention — careless judgments are punished.
- The server silently knows the truth but **never tells anyone during the
  game**. Players are free to confirm wrong answers or reject correct
  ones — and they will eat the consequence according to the rules above.

### When the game ends

The host configures a window (e.g. 15–25 minutes). At game start the
server picks a random instant within that window and schedules the
guillotine. When it fires, all pending requests are swept according to
the rules above, scores are revealed, and every request — including
its true peptide and the validity flag — is shown to everyone.

### Team mode

When creating the game, the host can flip the mode toggle to "Teams"
and configure team names. In team mode:

- Players pick their team from a dropdown in the lobby.
- Each team has a shared "sent" stack and a shared "received" stack.
  Any teammate can submit a decryption, and any teammate can confirm
  or reject a pending decision.
- Requests are assigned to a random *opposing team* (not a specific
  person). Any member of that team can decrypt it in the app.
- Points go to the team ledger. The end-game reveal shows the team
  scoreboard, then individuals grouped by team.
- An audit trail records who initiated each request, who decrypted,
  and who confirmed/rejected — handy for post-game accountability
  ("Bob, why did you confirm that mess?").
- First-write-wins: if two teammates race to submit a decryption, the
  first wins and the second sees a benign error.

## Quick start (host)

```bash
git clone <your repo>      # or unpack the tarball
cd riborumble
pip install -r requirements.txt
python main.py             # or: uvicorn main:app --host 0.0.0.0 --port 8000
```

The server binds to `0.0.0.0:8000` by default (so other devices on your
LAN can reach it). Open `http://<host-lan-ip>:8000/` in a browser.

### Finding your LAN IP

- **macOS / Linux**: `ipconfig getifaddr en0` or `hostname -I`
- **Windows**: `ipconfig` → look for IPv4 of your active adapter

Share `http://<that-ip>:8000/?game=<game-id>` with players once you've
created the game. They open the link, type their name, and they're in.

## Quick start (players)

1. Click the host's link.
2. Switch to the "Join game" tab (it pre-fills the game ID).
3. Enter your name and hit Join.
4. Wait for the host to start.

## Optional codon table handout

DNA and translations are handled in-app. A printed codon-table cheat
sheet is still useful and is shown on the lobby screen. It is also
available at `GET /api/games/<game_id>/codon-table` if you'd rather
print from a terminal.

Default table (10 codons, 7 AAs + Stop):

```
ATG→Met  GTT→Val  TTT→Phe  GAA→Glu  AAA→Lys
CCC→Pro  CAT→His  GGG→Gly  TGG→Trp  TAA→Stop
```

## Hosting alternatives

LAN works in most homes and offices. If you're on a network that
isolates clients (some hotels, cafes, corporate guest WiFi), use either:

- **Tailscale**: install on host + all clients, share the host's tailnet
  IP. Works through any firewall.
- **ngrok**: `ngrok http 8000` → temporary public URL. Less ideal for
  many clients but fine for small groups.

The app uses plain WebSocket and HTTP — no TLS required on a LAN. If you
expose it publicly, put it behind a reverse proxy with HTTPS.

## Reconnects

Disconnects are handled gracefully: the same display name rejoining the
same game restores the player's state. Tabs and laptops dying is fine.

## Configuration

Environment variables read by `main.py`:
- `HOST` (default `0.0.0.0`)
- `PORT` (default `8000`)

Game-time configuration (set when creating the game in the UI):
- Number of players (2–20)
- End window in minutes (min, max)
- Codon table (defaults provided, override via the API)

## File layout

```
riborumble/
├── codon.py          # codon table, DNA validation, peptide canonicalization
├── game.py           # state machine, scoring (no network code)
├── main.py           # FastAPI app: REST + WebSocket + static
├── static/index.html # single-page frontend
├── requirements.txt
└── README.md
```

## Troubleshooting

- **Players can't reach the host**: confirm host firewall allows port
  8000. On macOS: System Settings → Network → Firewall → Options. On
  Windows, the first run of `python main.py` will trigger a UAC prompt.
- **Game doesn't auto-end**: check the host terminal for tracebacks.
  The end timer is an `asyncio.Task`; if you ctrl-C the server, it's
  gone. Restart resets all state.
- **Someone joined with the wrong name**: there is no admin removal in
  this build. Restart the server. (For a friends-and-pizza party this
  has never been a real issue.)
