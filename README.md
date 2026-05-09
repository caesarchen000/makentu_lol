# Walkthrough — Tactical Voice + Console Integration

## What Was Built

A complete integrated system connecting a **Desktop UI** → **Python voice client** → **RPi router server** → **Loupedeck Creative Console plugin**.

## Files Changed

### New Files
| File | Purpose |
|------|---------|
| [tactical_ui.py](file:///d:/Documents/makentu_lol/tactical_ui.py) | Desktop tkinter UI — 4 screens: Connect → Pick Role → Pick Heroes → Dashboard |
| [tactical_config.json](file:///d:/Documents/makentu_lol/tactical_config.json) | Runtime config written by UI, read by client |
| [AllyChannelState.cs](file:///d:/Documents/makentu_lol/DemoPlugin/DemoPlugin/AllyChannelState.cs) | Static state manager for whisper set + ally/enemy config |
| [AllyChannelCommand.cs](file:///d:/Documents/makentu_lol/DemoPlugin/DemoPlugin/AllyChannelCommand.cs) | 5 ally toggle buttons for Creative Console (slots 1-5) |
| [protocol.py](file:///d:/Documents/makentu_lol/protocol.py) | Shared protocol definitions for all UDP message types |

### Modified Files
| File | Changes |
|------|---------|
| [tactical_client_cloud.py](file:///d:/Documents/makentu_lol/tactical_client_cloud.py) | Complete rewrite — multi-target whisper routing, Whisper+OpenAI pipeline, enemy alert broadcast |
| [router_server.py](file:///d:/Documents/makentu_lol/router_server.py) | Added CMD: packet detection and broadcast to all clients |
| [CountdownSignalListener.cs](file:///d:/Documents/makentu_lol/DemoPlugin/DemoPlugin/CountdownSignalListener.cs) | Handles CONFIG: messages (ally roles, enemy heroes) and CMD:ENEMY_ALERT for countdown triggers |

### Deleted Files
| File | Reason |
|------|--------|
| PushToTalkJGCommand.cs | Replaced by AllyChannelCommand system |
| PushToTalkAllCommand.cs | Replaced by AllyChannelCommand system |

## Data Flow

```
1. User launches tactical_ui.py
2. UI: Connect → Pick Role (e.g. MID) → Pick Enemy Heroes → Start
3. Writes tactical_config.json + launches tactical_client_cloud.py
4. Client reads config, registers with RPi ("HELLO:MID"), sends CONFIG to Loupedeck plugin
5. Console buttons update: Page 1 = 4 ally channels, Pages 2-3 = 10 enemy heroes

During gameplay:
6. Press ally button on console → PTT_ALLY3_TOGGLE → toggles that ally in whisper set
7. Hold PTT → audio streams to RPi → routed to whisper targets (or ALL)
8. Release PTT → Whisper transcribes → OpenAI analyzes:
   - "chat" → types message in game
   - "status_report" → CMD:ENEMY_ALERT broadcast → all consoles start countdown
```

## Verification

- ✅ C# plugin builds: `dotnet build` → 0 warnings, 0 errors
- ✅ Protocol module tests pass
- ✅ tkinter available for UI

## How to Run

1. **RPi**: `python router_server.py`
2. **Each PC**: `python tactical_ui.py` → follow setup screens
3. **Compile to .exe** (optional): `pyinstaller --onefile --windowed tactical_ui.py`
