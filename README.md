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
  in the app. If the DNA itself is invalid, flag it instead.
- If the decrypter submits a peptide, the requester then **confirms** or
  **rejects** the decryption.
- Everyone sees only their own live score (or their team's live score).
  The full leaderboard stays hidden until the end.

### Scoring

During the game:

Every score event is multiplied by:

```
m = 1 + complete_codon_count / 10
```

So a 5-amino-acid request has `m = 1.5`, and a base `+3` becomes `+4.5`.

| Requester's call | DNA | Submission | Decrypter | Requester |
|---|---|---|---|---|
| Confirm | valid | correct | **+3m** | **+1m** |
| Confirm | valid | wrong | **−3m** | **−3m** |
| Confirm | invalid | peptide submitted | **−3m** | **−6m** |
| Reject | valid | correct | **+3m** | **−3m** |
| Reject | valid | wrong | **−3m** | **+1m** |
| Reject | invalid | peptide submitted | **−3m** | **−2m** |

Receiver-side invalid flag:

| Receiver's call | DNA | Receiver | Requester |
|---|---|---|---|
| Flag invalid | invalid | **+1m** | **−3m** |
| Flag invalid | valid | **−3m** | 0 |

At game end (sweep):

| Pending state | Effect |
|---|---|
| Request never decrypted | Decrypter **−2m** |
| Invalid request never decrypted | Decrypter **−2m**, requester **−3m** |
| Correct submission never resolved | Decrypter **+2m**, requester **−2m** |
| Wrong submission never resolved | Decrypter **−2m**, requester **−2m** |
| Invalid request with unresolved peptide submission | Decrypter **−2m**, requester **−5m** |

Two key consequences:
- Decryption is graded against the truth, not against the requester's
  decision. A correct peptide is worth **+3m** even if rejected; a wrong
  peptide costs **−3m** even if accepted.
- Requester QC is graded separately. Good QC is worth **+1m**; bad QC
  costs **−3m**.
- Sending invalid DNA carries its own **−3m** penalty. If the sender also
  accepts a bogus peptide for that invalid request, both mistakes count.
- Decrypters can flag invalid DNA directly. Correct flags earn **+1m**
  and punish the sender; false flags cost the decrypter **−3m**.
- The server silently knows the truth but does not reveal the true peptide
  or validity flag during the game. Live score changes can still expose
  the consequence of a decision to the affected player/team.

### When the game ends

The host configures a fixed duration (e.g. 15 minutes). At game start the
server schedules the guillotine for exactly that many minutes later. When
it fires, all pending requests are swept according to the rules above,
scores are revealed, and every request — including its true peptide and
the validity flag — is shown to everyone.

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
- During play, teammates see only their own team's live score, not the
  whole leaderboard.
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

Important: run a single server process/worker. Game state lives in
process memory, so multiple Uvicorn workers would split players across
different game instances.

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

Default table (10 codons, 9 AAs + Stop):

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
- `GAME_CLEANUP_AFTER_SECONDS` (default `21600`; set to `-1` to keep
  ended games in memory until restart)

Game-time configuration (set when creating the game in the UI):
- Game duration in minutes
- Codon table (defaults provided, override via the API)

Runtime limits:
- Player names: 40 characters
- Players per game: 100
- Team names: 32 characters
- Protein names: 60 characters
- DNA requests: 300 bases after whitespace removal
- Peptide guesses: 400 characters
- Team colors: hex colors only, e.g. `#58a6ff`
- Custom codon tables: at most 64 codons, using amino acids supported by
  the peptide parser

## Testing

The test suite uses Python's standard `unittest` runner, so no extra test
dependency is required.

```bash
venv/bin/python -m unittest discover -v
```

The suite covers codon normalization/translation, custom table validation,
peptide matching, solo and team state transitions, every scoring-table
outcome, invalid-DNA flagging, end-game sweep scoring, reconnect-after-end
reveal behavior, cleanup of ended games, private live-score payloads, API
helper behavior, and frontend JavaScript syntax when `node` is available.

## File layout

```
riborumble/
├── codon.py          # codon table, DNA validation, peptide canonicalization
├── game.py           # state machine, scoring (no network code)
├── main.py           # FastAPI app: REST + WebSocket + static
├── static/index.html # single-page frontend
├── tests/            # unittest coverage for codon, game, API, and frontend glue
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
