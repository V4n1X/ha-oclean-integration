# OCLEANY3S – BLE-Sequenz: APK vs. Integration

Byte- und Ablauf-Abgleich der Bluetooth-Kommunikation für **OCLEANY3S**
(Oclean X Pro (S), APK-Protokoll-ID 9, Case 14, Handler `g.w0` Modus 1).

Zweck: Die Firmware der Bürste ist fragil — ein Live-Test hat sie in einen
Hänger gebracht (siehe `OCLEANY3S-AUDIT.md` §2.11). Deshalb muss der BLE-Verkehr
**1:1 dem der offiziellen App** entsprechen. Dieses Dokument ist die Checkliste
dafür; jede Zeile ist mit `Datei:Zeile` belegt.

Quellen: `OCleanResearch/_work/jadx/sources/…` (dekompilierter APK-Quellbaum),
Fallback-Gegenprobe `OCleanResearch/_work/jadx_fallback/…`.

---

## 1. Verbindungsaufbau

| Schritt | APK | Beleg | Integration |
|---|---|---|---|
| 1 | `connectGatt(context, false, callback, 2)` (API ≥ 26 zusätzlich `transport=2` = LE) | `i/a.java:182-189` | `establish_connection` (bleak) |
| 2 | Services entdecken und abwarten | `a0/d.java:98-99` → `i/a.java:c()` (wirft bei Fehler) | bleak nach `connect()` |
| 3 | **MTU anfordern**: `requestMtu(123)` bei „max"-Modus, sonst `23`; bei Fehlschlag Fallback auf 23 | `a0/d.java:72`, `:100-103`; `MtuSize.java` (`DEFAULT(23)`, `MAXIMUM(123)`) | **nicht** angefordert (OS-Verhandlung) |
| 4 | Handler erzeugen + Notifications aktivieren (`j(...)`) | `a0/d.java:104-105` | `_subscribe_battery_notifications` + `_subscribe_notifications` |
| 5 | `Thread.sleep(200)` | `a0/d.java:108` | `BLE_POST_CONNECT_DELAY` (2,0 s) |
| 6 | Batterie lesen (falls ≤ 0 oder > 100) | `a0/d.java:109-114` | `_read_battery_and_unsubscribe` |

> **Abweichung (akzeptiert):** Schritt 3. Die MTU wird nicht explizit
> angefordert. Auf Windows/bleak und über ESPHome-Proxys verhandelt das System
> eine große MTU, sodass alle Antworten in ein Notification-Paket passen.
> Sollte eine Umgebung bei MTU 23 landen, muss die `#`-Rahmung fragmentiert
> reassembliert werden (siehe §5) — aktuell **nicht** implementiert.

---

## 2. Notification-Abonnements

Die APK aktiviert in dieser Reihenfolge (`g/w0.java:81-85`):

| # | Characteristic | APK-Aufruf |
|---|---|---|
| 1 | `00002a19` (Battery) | `W(BATTERY_SERVICE_UUID, BATTERY_CHARACTER_UUID)` |
| 2 | `5f78df94-…bb86` (READ_INFO) | `W(OCLEAN_SERVICE_UUID, OCLEAN_READ_INFO_UUID)` |
| 3 | `5f78df94-…bb90` (RECEIVE_BRUSH) | `W(OCLEAN_SERVICE_UUID, OCLEAN_RECEIVE_BRUSH_UUID)` |

`…bb89` wird **nicht** abonniert (write-only). Der Helfer `W()`
(`g/e.java:379-397`) macht genau:

1. `setCharacteristicNotification(char, true)`
2. für jeden Descriptor: Wert = `ENABLE_NOTIFICATION_VALUE` (0x0001), wenn
   Property-Bit 4 (NOTIFY) gesetzt ist, sonst `ENABLE_INDICATION_VALUE`
   (0x0002), wenn Property-Bit 5 (INDICATE) gesetzt ist
3. `writeDescriptor(descriptor)`

**Kein Pre-Clear des CCCD** (`0x0000`) — das war eine reine Integration-Erfindung
und ist entfernt (nur noch ein Retry bei „Notify acquired"/Timeout).

Integration: Reihenfolge jetzt identisch (2A19 → fbb86 → fbb90),
`_subscribe_notifications` macht im Normalfall nur `start_notify`.

---

## 3. Zeitkalibrierung

| Punkt | APK | Integration |
|---|---|---|
| Kommando | `0201` + 8 Byte | `CMD_CALIBRATE_TIME_T1_PREFIX` |
| Ziel-Char | `f10134k` = `…bb85` | `protocol.write_char` = `…bb85` ✔ |
| Nutzlast | Jahr-2000, Monat, Tag, Std, Min, Sek, Wochentag (So=0), tzIndex (1-basiert) | identisch, `oclean_tz_index()` |
| Beleg | `g/w0.java:248-263` | `coordinator._calibrate_time` |

---

## 4. Kommandos: Ziel-Characteristic und Reihenfolge

**Regel der APK: alles außer `0307` geht auf `…bb85`, nur `0307` auf `…bb89`.**

| Kommando | APK-Ziel | Beleg | Integration |
|---|---|---|---|
| `0303` Status | `…bb85` | `g/w0.java:324`, `:328` | `(WRITE_CHAR_UUID, CMD_QUERY_STATUS)` ✔ |
| `030201` Settings | `…bb85` | `g/w0.java:375`, `:379` | `(WRITE_CHAR_UUID, CMD_QUERY_DEVICE_SETTINGS)` ✔ |
| `0307` Running Data | `…bb89` | `g/w0.java:360`, `:364` | `(SEND_BRUSH_CMD_UUID, CMD_QUERY_RUNNING_DATA_T1)` ✔ |
| `0201` Kalibrierung | `…bb85` | `g/w0.java:248` | ✔ |
| `0206`/`020B` Schema | `…bb85` | `g/w0.java:62`, `:93` (`f10221x`) | ✔ |
| `020D` Area-Remind | `…bb85` | `g/w0.java:339-346` | ✔ |
| `020F` Kopf-Reset | `…bb85` | `g/w0.java:179` | ✔ |
| `0212` Over-Pressure | `…bb85` | `g/w0.java:194-201` | ✔ |
| `0217` Kopf-Max-Tage | `…bb85` | `g/w0.java:1311-1313` (Big-Endian) | ✔ |
| `0202` clearRunningDate | `…bb85` | `g/w0.java:503-515` | **nie gesendet** ✔ |
| `0239`/`0240` | existieren in `g.w0` **nicht** | nur `g/a.java`, `g/u0.java`, `g/b0.java` | Switches bleiben (andere Modelle) |
| `0309` Pagination | existiert in `g.w0` **nicht** | nur `g/a` Var. 0, `g/h`, `g/b1` | `supports_pagination=False` ✔ |
| `0308` (Type-0) | nicht für `g.w0` | – | nicht im Profil ✔ |

### 4.1 Schreibtyp und Chunking

| Punkt | APK | Beleg | Integration |
|---|---|---|---|
| Write-Typ | **Default = Write With Response** (nirgends `setWriteType`) | keine `setWriteType`-Treffer im gesamten Quellbaum | `response=True` ✔ |
| Chunk-Grenze | `MTU − 3` | `g/d.java:36-38` | Nutzlast ≤ 9 Byte, irrelevant |
| Chunk-Reihenfolge | sequenziell, je Chunk auf Write-ACK warten | `a0/e.java:88-97`, `g/d.java:56-69` | ein Write pro Kommando ✔ |
| Write-ACK-Timeout | `sendTimeout` = **3000 ms** | `Options.I111111l`; `OcleanBleManager.java:160` | `BLE_WRITE_TIMEOUT` 5 s (bewusst großzügiger) |

### 4.2 Taktung (wichtig!)

Die App führt Kommandos **nicht** gleichzeitig aus:

```
g/e.java:632-642   jede Aktion -> single-thread Executor (f10139p)
g/d.java:56-69     writeCharacteristic(); warte auf Write-ACK (≤ 3 s)
g/d.java:70-153    warte auf die Antwort-Notification (receiveTimeout = 5000 ms)
a0/e.java:139      SystemClock.sleep(100) nach jedem Kommando
```

Integration (seit diesem Commit): nach jedem Kommando wird auf **eine**
Notification gewartet (`CMD_RESPONSE_WAIT` = 2,0 s) und danach `CMD_GAP` =
100 ms geschlafen. Vorher wurden alle Kommandos ohne Pause hintereinander
geschrieben — das ist die wahrscheinlichste Ursache für den Firmware-Hänger.

---

## 5. Antwort-Framing

### 5.1 `0303` – roh

`payload[0]` = Status, `payload[3]` = Kapazität/Batterie; **kein** Header.
`g/w0.java:1207-1215` liest direkt bei Offset 0 und 3. Reale Aufzeichnung:
`0303 02 0e d5 36 00 00` (Status 2, Batterie 0x36 = 54).
Integration: `_parse_state_response` liest `payload[3]` ✔

### 5.2 `0302` – `#`-Längenrahmen

Der Framer `w/a.java:26-80` verlangt:

```
value[0] == '#'            (bytesToAscii(value, 1) == "#")
value[1] == len(daten) + 2 (bytesToIntBe(value, 1, 2) − 2)
daten    = value[2 : 2+len]
```

und reassembliert Folgpakete bis `len(daten)` Bytes angekommen sind.
Aufgerufen wird er ausschließlich für `0302` (`g/w0.java:659`, `:1220`) mit
`bArr8` = Nutzlast **nach** dem 2-Byte-Opcode (`d0()` in `g/e.java:469-485`
schneidet `bytes[0:2]` ab).

Auf der Leitung steht also: `03 02 | 23 | <len+2> | <daten…>`.

Integration: `_strip_info_frame()` entfernt den Rahmen, wenn
`payload[0] == 0x23` **und** `payload[1] == len(payload)`; sonst wird roh
geparst (rückwärtskompatibel). Damit stimmen die Offsets des `w0`-Layouts
(§`OCLEANY3S-AUDIT.md` 2.5).

> **Offen:** Bei MTU 23 (Fragmente > 20 Byte) muss über mehrere Notifications
> reassembliert werden. Nicht implementiert, weil alle unterstützten Umgebungen
> eine große MTU verhandeln.

### 5.3 `0307` – `*B#`-Stream

`w/b.java:285-350` (`e()`), verwendet von `g/w0`:

```
Header : "0307" + ASCII "*B#"  (03 07 2a 42 23)
Anzahl : 2 Byte BE ab Offset 5
Größe  : Anzahl × 42
Daten  : ab Offset 7
Folge  : rohe Pakete ohne Header-Prüfung, bis Anzahl × 42 erreicht
Zustand: 10 = läuft, 11 = fertig, 12 = Fehler
```

Integration: `_make_notification_handler` prüft `data[2:5] == b"\x2a\x42\x23"`,
liest die Anzahl bei `payload[3:5]`, nimmt `payload[5:]` als Startpuffer und
akzeptiert Folgpakete ohne Header-Prüfung ✔ — identisch.

### 5.4 `2A19` – Batterie

`bytesToIntBe(bArr)` über das ganze Paket (`g/w0.java:1299-1301`) → ein Byte.
Integration: `parse_battery(bytes)` ✔

### 5.5 `0201`/`0202` – ACK

Die letzten 2 Byte als ASCII = `"OK"` (`g/w0.java:1290-1292`, `:1123-1125`).
Integration: `0202` → `_handle_device_info_ack` (nur Log) ✔; `0201`-Antwort
wird nicht ausgewertet (nur Log über den Unknown-Pfad) — unkritisch.

---

## 6. Was die Integration zusätzlich tut (Restrisiko)

| Zusatz | Bewertung |
|---|---|
| `_read_response_char_fallback` (mehrfaches `read_gatt_char` auf fbb86) | nur wenn `start_notify` fehlschlägt |
| `_poll_receive_brush_fallback` (Polling auf fbb90) | nur wenn `start_notify` fehlschlägt |
| CCCD-Retry mit `0x0000` | nur bei „Notify acquired"/Timeout |
| `_paginate_sessions` | nur bei `supports_pagination` (für Y3S false) |
| Verbindungsaufbau ohne MTU-Request | siehe §1 |
| Enrichment-Wartezeit 1,5 s nach Sessions | reines Warten, kein Funkverkehr |

Alle Zusätze sind **reaktiv** (nur nach einem Fehler) und damit im Normalbetrieb
nicht wirksam.

---

## 7. Checkliste für den nächsten Hardware-Test

1. Kein `pair()` / `unpair()` während des Tests (Windows-Kopplung vorher
   manuell entfernen, falls sie stört).
2. Nur **eine** Verbindung pro Versuch, danach sauber trennen.
3. Reihenfolge: verbinden → DIS lesen → 2A19/fbb86/fbb90 abonnieren →
   `0201` → `0303` → `030201` → `0307`.
4. Kommandos **nie** parallel, immer mit Antwort-Wartezeit (Skript macht das).
5. Abbruch bei der ersten unerwarteten Antwort (`Write Not Permitted`,
   `Unreachable`) — nicht wiederholen, sonst riskiert man den Hänger erneut.
6. Rohdaten mitschreiben (`--out report.json`) und **nicht** committen.

Werkzeug: `tools/oclean_live_check.py` (implementiert genau diese Sequenz).
