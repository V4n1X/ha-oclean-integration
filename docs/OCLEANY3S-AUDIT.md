# OCLEANY3S-Audit – Abgleich der Integration gegen die APK-Analyse

**Gegenstand:** `custom_components/oclean_ble` (dieser Fork) gegen die statische
Analyse der **OClean Care+ 4.0.4** (`com.yunding.noopsychebrushforeign`,
`versionCode 114`, `targetSdkVersion 35`) aus `OCleanResearch/`.

**Schwerpunkt:** Modell **OCLEANY3S** (Oclean X Pro (S)) – APK `DeviceType`
`OCLEAN_Y3S`, **Protokoll-ID 9**, Case 14.

**Zielplattform:** Home Assistant **2026.9.1** (Release 2026-09-05, benötigt
Python ≥ 3.14.2).

**Belegdisziplin:** Jede Aussage ist mit `Datei:Zeile` belegt – entweder aus dem
dekompilierten Quellbaum (`OCleanResearch/_work/jadx/sources/…`,
Fallback-Variante `OCleanResearch/_work/jadx_fallback/…`) oder aus den
Lieferdokumenten `OCleanResearch/0X_*.md`. Nicht belegbare Punkte sind als
**UNBELEGT** markiert.

---

## 1. Geräteidentität und Handler-Zuordnung

| Merkmal | Wert | Beleg |
|---|---|---|
| Protokoll-ID | **9** | `com/ocleanble/lib/device/DeviceType.java:147` |
| `deviceName` | `OCLEANY3S` | ebd. |
| Case in `i/d.java` | **14** | `i/d.java:69-72` |
| Protokollklasse | **`g.w0` mit Modus-int 1** | `i/a.java:320-341` (Konstruktion `new w0(..., 1)`, Zeile 339) |
| OTA-Verfahren | `Dialog` / SPOTA | `i/a.java:334` |
| `dentalCast` (Zonen) | **8** | `cc/a.java:15` |
| `isNewValidBrushTime` | **false** | ebd. |
| `hasAreaRemind` | **true** | ebd. |
| `hasOverPressure` | **true** | ebd. |
| `hasTheme` / `hasPoint` / `hasRaiseWake` | **false** | ebd. |
| `hasLanguageMode` / `hasWeather` | **true / false** | ebd. |

**Wichtig:** `OCLEANY3S` teilt sich den Handler mit `OCLEANY3`, `OCLEANY3T`,
`OCLEANY3M*`, `OCLEANY3N`, `OCLEANY3D*` und `OCLEANR3L` (Cases 13–25).
`OCLEANY3P`/`OCLEANY3PD` liegen dagegen in **Cases 26/27 → `g.g` Modus 0**
(`i/a.java:342-352`) und haben ein *anderes* 0302-Layout.

### 1.1 Umgesetzte Änderung

`protocol.py` hat jetzt ein eigenes Profil **`TYPE1_Y3`** („Type-1
(Y3/C3385w0)") für die `g.w0`-Familie; `OCLEANY3S`, `OCLEANY3`, `OCLEANY3T`,
`OCLEANY3M*`, `OCLEANY3N`, `OCLEANY3D*`, `OCLEANR3L` und `OCLEANY3MH` zeigen
darauf. `TYPE1` bleibt für die `g.g`-Familie (`OCLEANY3P`, `OCLEANY3PB`,
`OCLEANY3PD`, `OCLEANX20`, `OCLEANV1a`) erhalten.

---

## 2. Fehler, die behoben wurden

### 2.1 `0202` war als „Device Info" abonniert – es ist `clearRunningDate` ⚠️

**Befund:** `CMD_DEVICE_INFO = 0x0202` stand in *allen* `query_commands` und
wurde damit **bei jedem Poll** an die Bürste gesendet.

Die APK zeigt, dass `0x0202` **kein** Abfragekommando ist:

* `g/w0.java:503-515` (`r0(listener)`) schreibt `0202`; identisch in
  `g/g.java:1011-1027`, `g/s.java:487`, `g/u0.java:619`, `g/b0.java:485`,
  `g/h.java:293`, `g/f.java:352`, `g/a.java:535`, `g/b1.java:268`,
  `g/a1.java:239`, `g/y0.java:194`, `g/z0.java:172`, `g/d1.java:455`.
* Die öffentliche SDK-Fassade bindet genau diese Methode als
  **`clearRunningDate(mac, listener)`**: `com/ocleanble/lib/OcleanBleManager.java:677-698`.
* Die App ruft sie ausschließlich **nach** dem Session-Upload auf:
  `com/yunding/noopsychebrushforeign/page/device/OcleanDataService.java:663-664`
  und `:704`.
* Das eigentliche Geräte-Info-Kommando ist `030201` (`g/w0.java:375`, `:379`).

**Risiko:** Ein pollendes `0202` signalisiert der Bürste, dass die Putzdaten
„verbraucht" sind. Je nach Firmware kann das nicht synchronisierte Sitzungen als
erledigt markieren (oder löschen). Dass die Integration bisher trotzdem Daten
sah, liegt vermutlich daran, dass das Gerät das Kommando ohne weitere
Parameter ignoriert – verlassen sollte man sich darauf nicht.

**Fix:** `0202` aus **allen** `query_commands` entfernt; Konstante umbenannt zu
`CMD_CLEAR_RUNNING_DATA` mit erklärendem Kommentar. Die ACK-Antwort wird
weiterhin über `RESP_DEVICE_INFO` verarbeitet.

### 2.2 `gestureCode` stand an der falschen Stelle

**Vorher:** `(record[30] >> 2) & 0x3` (2 Bit aus Byte 30).
**APK:** `gestureCode` ist das **vollständige Byte 18** – es ist zugleich
`gestureArray[0]`:

```java
// g/w0.java:979-980   iBytesToIntBe61 = bytesToIntBe(bArr6, 18, 19)
// g/w0.java:1087      .put("gestureCode", iBytesToIntBe61)
```

**Fix:** `result[DATA_LAST_BRUSH_GESTURE_CODE] = record[18]` (0–255).
Die Entität `last_brush_gesture_code` ist standardmäßig deaktiviert
(diagnostisch), daher ist die Auswirkung gering – der Wert war vorher aber
schlicht ein `powerArray`-Nibble und damit bedeutungslos.

### 2.3 `gestureArray` hatte Länge 12 statt 13 und die falsche Basis

**APK (maßgeblicher Zweig):** Diese APK setzt `VALIDATE="false"`
(`AndroidManifest.xml`, siehe `02_BLE_Stack_Verbindung.md:184-185`), wodurch
`f10129f == false` gilt und in `g/w0.java:1034-1073` der **`else`-Zweig** läuft.
Dieser baut `gestureArray` aus den **Bytes 18–30 (13 Werte)**:

```java
// g/w0.java:1034-1046
a.b.l(sb6, iBytesToIntBe61, iBytesToIntBe62);   // Bytes 18,19
a.b.l(sb6, iBytesToIntBe63, iBytesToIntBe64);   // Bytes 20,21
a.b.l(sb6, iBytesToIntBe65, iBytesToIntBe66);   // Bytes 22,23
a.b.l(sb6, iBytesToIntBe67, iBytesToIntBe68);   // Bytes 24,25
a.b.l(sb6, iBytesToIntBe69, iBytesToIntBe70);   // Bytes 26,27
sb6.append(iBytesToIntBe71); …                  // Bytes 28,29,30
```

Die Integration lieferte `record[23:31] + [0,0,0,0]` (12 Werte) – der
Docstring in `const.py` behauptete dagegen „len=13 (bytes 18-30)". Der Wert war
also intern widersprüchlich.

**Fix:** `gesture_array = list(record[18:31])` (13 Werte).
**Wichtig:** Die Zahnzonen sind der **Schwanz** dieser Liste – siehe 2.4 –
`last_brush_areas` und `last_brush_coverage` bleiben deshalb **unverändert**.

### 2.4 Verifikation der Zahnzonen (kein Fehler, aber Gegenprobe)

Die 8 Zonen stammen aus `com/google/firebase/b.java:1020-1034`:

```java
public static int[] z(String[] strArr, int i10, cc.a aVar) {
    int i13 = strArr.length > 12 ? 1 : 0;
    fArr[0] = Integer.parseInt(strArr[i13 + 4]);
    …
    fArr[7] = Integer.parseInt(strArr[i13 + 11]);
```

Bei 13 Elementen (`i13 = 1`) sind die Zonen also **Indizes 5–12 = Bytes 23–30**.
Der bisherige Zugriff auf Bytes 23–30 war damit korrekt; er ist jetzt als
`gesture_array[5:13]` formuliert und kommentiert.

**Schwellwert für OCLEANY3S = 9,0** (`com/google/firebase/b.java:1091-1099`;
Sonderfälle: `Y3PD → 10,0` bei `:1073`, `YD0003 → 8,0` bei `:1082`). Die
Integration setzt das bereits korrekt um.

### 2.5 `0302`-Layout: Batterie und Kopfzähler waren verschoben ⚠️

Die APK parst die 0302-Antwort **pro Protokollklasse**. Für `g.w0` (also auch
OCLEANY3S) gilt `g/w0.java:1219-1272`:

| Offset | Bedeutung | von der Integration vorher gelesen als |
|---|---|---|
| 0 | `deviceTheme` | ❌ **Batterie** |
| 4–7 | Voice-Typ-Wort | – |
| 8 / 9 / 10 | volumeSwitch / volume / calendarSwitch | – |
| 11 | `pNum` | – |
| 12 | `brushMode` | – |
| 15 | `headUsedTimeLong` (1 Byte) | – |
| 16–22 | Geräteuhr (Jahr+2000, M, T, h, m, s) | – |
| 23 | `overPressure` | – |
| 24 | `areaRemind` | – |
| 25 | Zeitzonen-Index | – |
| 25–26 | `headMaxTimeLong` | ✅ (identisch) |
| 27–28 | `headUsedDays` | ❌ als `headUsedTimeLong` |
| 29–30 | `headUsedTimes` | ❌ als `headUsedDays` |
| 31 | `deviceLanguage` | ❌ als `headUsedTimes` |

**Auswirkung für OCLEANY3S:**
* `brush_head_usage` zeigte den **Sprachcode** (0–5) statt der Sitzungszahl.
* `brush_head_days` zeigte die **Sitzungszahl** statt der Kalendertage.
* `brush_mode` zeigte Byte 5 = Teil des Voice-Typ-Worts (praktisch immer 0).
* `battery` konnte aus Byte 0 = `deviceTheme` überschrieben werden (0–100 ist
  ein plausibler Batteriebereich!) – der endgültige Wert kam zwar aus `2A19`,
  bei fehlgeschlagenem 2A19-Read wäre aber der Theme-Wert stehengeblieben.

**Fix:** Neues Feld `DeviceProtocol.settings_layout` (`SETTINGS_LAYOUT_W0` /
`SETTINGS_LAYOUT_GENERIC`); `parser._parse_device_settings_response()` wertet
beide Layouts aus. `TYPE1_Y3` **und** `LEGACY` (Air-1-Familie läuft laut APK im
selben `g.w0`-Handler, Modus 0) nutzen `w0`, alle anderen Profile das
bisherige generische Layout.

### 2.6 `device_registry.async_get_device(identifiers=…)` ist ab HA 2026.9 veraltet

**Befund:** In HA 2026.9 als deprecated markiert (Custom-Integrationen warnen
bis 2027.8). **Fix:** bevorzugt `async_get_device_id_by_identifier((DOMAIN, mac))`
mit Rückfall auf die alte API, solange ältere Cores unterstützt werden
(`coordinator.py`, DIS-Registry-Update).

### 2.7 Langzeitstatistiken: `has_mean` ist ab HA 2026.11 ungültig

**Befund:** `StatisticMetaData(has_mean=True, …)` ohne `mean_type`. Ab HA 2026.7
ist `mean_type` erforderlich; das Weglassen „funktioniert ab Home Assistant Core
2026.11 nicht mehr".
**Fix:** `mean_type=StatisticMeanType.ARITHMETIC` + `unit_class`
(`"unitless"` für `%`/dimensionslos, `"duration"` für Sekunden). Auf älteren
Cores ohne `StatisticMeanType` fällt der Code automatisch auf `has_mean` zurück.

### 2.8 Testsuite: `/tmp` unter Windows

`tests/test_init.py` benutzte `os.environ.get("TMPDIR", "/tmp")`. Unter Windows
löst das zu `C:\tmp` auf, das üblicherweise nicht schreibbar ist → 8 Tests
schlugen mit `PermissionError` fehl. **Fix:** `tempfile.gettempdir()`.

---

## 3. Befunde ohne Codeänderung (dokumentiert, bewusst nicht geändert)

### 3.1 Kommandopfad: APK nutzt für 0303/030201 `…bb85`, die Integration `…bb89`

`g/w0.java:324`/`:328` (0303), `:375`/`:379` (030201) schreiben auf
`f10134k` = `9d84b9a3-…bb85`; **nur** `0307` geht auf `y` =
`5f78df94-…bb89` (`:360`/`:364`). Die Integration sendet alle vier Kommandos
über `fbb89`.

**Nicht geändert**, weil die Feldprotokolle (Issue #49, #37) zeigen, dass die
Geräte auf `fbb89` antworten – viele Oclean-Firmwaren akzeptieren offenbar
beide Schreib-Characteristics. Eine Umstellung ohne Hardware-Test wäre ein
Risiko. Als **Folgeaufgabe** dokumentiert: wer ein OCLEANY3S zur Hand hat, kann
die APK-Variante testen.

### 3.2 `0239` / `0240` existieren in `g.w0` nicht

Die Switches „Brushing Reminder" (`0239`) und „Auto Power-Off Timer" (`0240`)
werden für **alle** Modelle angeboten. Die APK kennt `0239` nur in
`g/a.java:319` und `g/u0.java:191`, `0240` nur in `g/a.java:364` und
`g/b0.java:908` – **nicht** in `g/w0`. Für OCLEANY3S sind diese Kommandos damit
voraussichtlich wirkungslos.

**Nicht geändert**, weil die Entitäten bei anderen Modellen (empirisch)
verwendet werden und ein Entfernen eine Regression wäre. Als Folgeaufgabe
notiert: modellabhängige Verfügbarkeit über die `cc.a`-Fähigkeitsmatrix
(`hasAreaRemind`, `hasOverPressure`, …) statt globaler Entitäten.

### 3.3 pNum-/Schema-Liste ist nicht aus der APK belegbar

Die APK enthält **keine** Schema-Tabelle: `pNum < 230` → BrushPlan, sonst
WashScheme (`kg/a.java:112-120`); die Zeilen liegen in einer Room-Tabelle mit
Spalte `DeviceType` (`rc/a.java:93`) und werden pro Modell nachgeschlagen
(`tc.b.e(schemeId, deviceModel)`), d. h. **cloud-/DB-geliefert**
(`GET /Romap/v1/DeviceContoller/GetAllResources`).

`SCHEMES_BY_MODEL["OCLEANY3S"] = OCLEANY3_SCHEMES` (also `OCLEANY3M_SCHEMES` +
pNum 90) ist damit **UNBELEGT**; der eigene Kommentar in `const.py` bezeichnet
pNum 90 als exklusiv für `OCLEANY3`. Die Zuordnung bleibt bestehen (kein
bekannter Schaden), ist aber jetzt als unbestätigt kommentiert.

### 3.4 `0309`-Pagination für die `g.w0`-Familie

`0309` kommt in `g.w0` nicht vor (nur `g/a` Var. 0, `g/h`, `g/b1`).
`TYPE1_Y3.supports_pagination = False` war bereits korrekt.

### 3.5 Coverage-Prozentsatz der App

Der von der App **angezeigte** Prozentwert ist das Cloud-Feld `clean`
(`BrushRecordResult.getClean()`), das im BLE-Record nicht vorkommt. Der
berechnete Wert reproduziert die **Diagramm-Klassifikation** (Stufen 1/3) – das
ist die nächstliegende lokale Entsprechung, nicht der Cloud-Wert.

### 3.6 Switch-Rücklesung aus 0302 (möglich, nicht umgesetzt)

Das `w0`-Layout liefert `overPressure` (Byte 23) und `areaRemind` (Byte 24)
als **Ist-Werte**. Die Integration führt diese Schalter bisher als
`assumed_state` und speichert sie lokal. Eine Rücklesung würde den echten
Gerätezustand zeigen – das ist eine sinnvolle, aber verhaltensändernde
Folgeaufgabe (Switch-Semantik, Persistenz, `assumed_state`).

### 3.7 `PERCENTAGE` als Einheit

HA hat `PERCENTAGE` ab 2026.7 als Einheit von Sensoren als veraltet markiert.
Ein Ersatzname ist in der offiziellen Quelle nicht benannt; solange keiner
dokumentiert ist, bleibt `PERCENTAGE` bestehen (kein Erfinden eines Ersatzes).

---

## 4. Home-Assistant-2026.9.1-Kompatibilität

| Punkt | Status |
|---|---|
| Zielversion | **2026.9.1** (2026-09-05), Python ≥ 3.14.2 |
| `hacs.json` | `homeassistant: "2026.9.0"`; nicht unterstützter Key `render_readme` entfernt |
| CI | Python **3.14**, `homeassistant==2026.9.1` (Pin mit Marker `python_version >= "3.14"`) |
| `pyproject.toml` | `target-version = py314`, `mypy.python_version = 3.14` |
| `manifest.json` | `integration_type: "device"`, `loggers: ["custom_components.oclean_ble"]` |
| `DataUpdateCoordinator` | erhält jetzt `config_entry=entry` |
| `ConfigFlowResult` | aus `homeassistant.config_entries` statt `FlowResult` |
| `UnitOfTime.DAYS` | statt String `"d"` |
| Brand-Assets | `brand/dark_icon.png` + `@2x` ergänzt; redundantes Altverzeichnis `images/` entfernt |
| `pytest.ini` | `asyncio_default_fixture_loop_scope = function` |
| `bleak_retry_connector` | **nicht** veraltet – weiterhin gepinnte Core-Abhängigkeit des `bluetooth`-Components in 2026.9.1 |

### 4.1 Grenzen der Prüfung

* Lokal installiert ist HA **2026.2.3 / Python 3.13.14**, nicht 2026.9.1
  (2026.9.1 verlangt Python ≥ 3.14.2). Die Aussagen zu 2026.9.1 stammen aus dem
  Core-Quellbaum am Tag `2026.9.1` und der offiziellen Dokumentation, nicht aus
  einer lokalen Installation.
* Die Unit-Tests stubsen alle `homeassistant.*`-Module
  (`tests/conftest.py`) und laufen daher ohne echte HA-Installation. Der
  `homeassistant`-Pin in `requirements-test.txt` stellt sicher, dass CI gegen
  die Zielversion auflöst; die Tests selbst bleiben hermetisch.
* Nicht lokal verifizierbar: `mypy` (nicht installiert), `hassfest`,
  echte BLE-Hardware.

---

## 5. Nicht verifizierbare Punkte (UNBELEGT)

| Punkt | Status |
|---|---|
| pNum-90-Schema für OCLEANY3S | UNBELEGT (Cloud-Liste, siehe 3.3) |
| Ob OCLEANY3S-`0202` Daten wirklich löscht | UNBELEGT; Kommando ist laut APK `clearRunningDate`, Verhalten der Firmware ohne Parameter unbekannt |
| Ob OCLEANY3S Schreibzugriffe auf `fbb85` ebenso akzeptiert wie `fbb89` | UNBELEGT (siehe 3.1) |
| Bedeutung von `gestureCode` (Byte 18) | UNBELEGT – die App speichert/überträgt den Wert, zeigt ihn aber nie an |
| Bedeutung von `powerArray` | UNBELEGT – 12 × 2 Bit, keine 0–3→Label-Zuordnung in der APK |

---

## 6. Reproduktion der Belege

```powershell
# Dekompilierter Quellbaum (jadx)
OCleanResearch\_work\jadx\sources\g\w0.java              # g.w0 – Y3S/Y3M/Y3/A1-Handler
OCleanResearch\_work\jadx\sources\com\google\firebase\b.java   # z() – 8-Zonen-Diagramm
OCleanResearch\_work\jadx\sources\cc\a.java             # Fähigkeitsmatrix pro Modell
OCleanResearch\_work\jadx\sources\i\a.java              # Case → Handler-Klasse
OCleanResearch\_work\jadx\sources\com\ocleanble\lib\OcleanBleManager.java  # clearRunningDate
OCleanResearch\_work\jadx_fallback\g_w0.fallback.java   # Gegenprobe (rohe DEX-Instruktionen)

# Tests
python -m pytest tests/test_ocleany3s.py -v
```

Die 25 Tests in `tests/test_ocleany3s.py` prüfen genau die in diesem Dokument
beschriebenen Punkte: Modellzuordnung, Kommandosequenz ohne `0202`,
`w0`-0302-Layout, Record-Offsets (Byte 18 / 23–30) und den Schwellwert 9.
