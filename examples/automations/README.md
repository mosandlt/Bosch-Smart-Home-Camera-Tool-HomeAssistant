# Example Automations / Beispiel-Automationen

> Drop-in YAML snippets for common Bosch camera scenarios. Copy what you need into your `automations.yaml` (or via the UI editor → *three-dot menu → Edit in YAML*) and replace the placeholder entity IDs with your own.

> Fertige YAML-Snippets für typische Bosch-Kamera-Szenarien. Kopiert was ihr braucht in eure `automations.yaml` (oder via UI-Editor → *Drei-Punkte-Menü → In YAML bearbeiten*) und ersetzt die Platzhalter durch eure Entities.

Every file contains both English (EN:) and German (DE:) explanations side by side. Code is portable — only entity IDs need to be adapted. / Jede Datei enthält EN: und DE: Erklärungen direkt nebeneinander. Code ist portabel — nur Entity-IDs anpassen.

## Index / Inhalt

### Motion-light control / Bewegungslicht-Steuerung

| File / Datei | Use case / Anwendungsfall | Camera gen / Kamera-Gen |
|---|---|---|
| [`motion-light-helper-switch.yaml`](motion-light-helper-switch.yaml) | EN: Manual "Dinner Mode" toggle in the dashboard. <br> DE: Manueller "Dinner-Mode"-Schalter im Dashboard. | Gen2 native, Gen1 via fallback |
| [`motion-light-door-sensor.yaml`](motion-light-door-sensor.yaml) | EN: Door / window opens → motion light off until 2 min after closing. <br> DE: Tür/Fenster auf → Bewegungslicht aus, 2 Min nach Schließen wieder an. | Gen2 native, Gen1 via fallback |
| [`motion-light-presence.yaml`](motion-light-presence.yaml) | EN: Any presence sensor (mmWave, PIR, BLE, …) on the porch suppresses light. <br> DE: Beliebiger Anwesenheitssensor (mmWave, PIR, BLE, …) auf der Terrasse unterdrückt das Licht. | Gen2 native, Gen1 via fallback |
| [`motion-light-time-schedule.yaml`](motion-light-time-schedule.yaml) | EN: Fixed time window OR sunset/sunrise-driven. <br> DE: Festes Zeitfenster ODER Sonnenuntergangs-/Sonnenaufgangs-getrieben. | Gen2 |
| [`motion-light-gen1-instant-off.yaml`](motion-light-gen1-instant-off.yaml) | EN: Gen1 fallback — react to motion event, instantly send light=off. <br> DE: Gen1-Fallback — auf Motion reagieren, sofort light=off senden. | **Gen1 only** |
| [`motion-light-gen1-power-cycle.yaml`](motion-light-gen1-power-cycle.yaml) | EN: Gen1 hardware cut — power the camera off via a smart plug. <br> DE: Gen1 Hardware-Trennung — Kamera per Smart-Plug abschalten. | **Gen1 only** |

### Privacy & away mode / Privacy & Abwesenheit

| File / Datei | Use case / Anwendungsfall |
|---|---|
| [`privacy-when-home.yaml`](privacy-when-home.yaml) | EN: Indoor cameras automatically privacy-on when anyone is home. <br> DE: Innenkameras automatisch auf Privacy wenn jemand zuhause ist. |
| [`away-mode-arm-cameras.yaml`](away-mode-arm-cameras.yaml) | EN: Arm all cameras when last person leaves; disarm when first returns. <br> DE: Alle Kameras scharfstellen wenn der Letzte geht; entschärfen wenn jemand zurück. |

### Notifications & integrations / Benachrichtigungen & Integrationen

| File / Datei | Use case / Anwendungsfall |
|---|---|
| [`snapshot-and-notify-on-motion.yaml`](snapshot-and-notify-on-motion.yaml) | EN: On motion: snapshot + push with image + "View live" action. <br> DE: Bei Motion: Snapshot + Push mit Bild + "Live ansehen"-Action. |
| [`weather-suppress-alerts.yaml`](weather-suppress-alerts.yaml) | EN: Skip push during high wind / heavy rain (less false-positives). <br> DE: Push überspringen bei starkem Wind / Regen (weniger Fehlauslöser). |
| [`doorbell-stream-on-tablet.yaml`](doorbell-stream-on-tablet.yaml) | EN: Wall-mounted tablet auto-displays the live stream on motion. <br> DE: Wandtablet zeigt den Live-Stream automatisch bei Bewegung. |
| [`sleep-mode-mute-cameras.yaml`](sleep-mode-mute-cameras.yaml) | EN: Quiet at night, but real intruder pattern (3+ events in 5 min) wakes you. <br> DE: Nachts ruhig, aber echtes Einbruchsmuster (3+ Events in 5 Min) weckt euch. |
| [`vacation-deterrent-light.yaml`](vacation-deterrent-light.yaml) | EN: Random camera light flashes during vacation — looks lived-in. <br> DE: Zufällige Lichtblitze im Urlaub — wirkt bewohnt. |
| [`garage-vehicle-coordination.yaml`](garage-vehicle-coordination.yaml) | EN: Garage door + driveway camera = "vehicle arriving / leaving" events, plus optional AI-driven vehicle ID (our car / delivery van / unknown). <br> DE: Garagentor + Einfahrt-Kamera = "Fahrzeug kommt/fährt"-Events, plus optional AI-Erkennung welches Fahrzeug. |

### AI & vision / AI & Vision

| File / Datei | Use case / Anwendungsfall |
|---|---|
| [`ai-vision-smart-alerts.yaml`](ai-vision-smart-alerts.yaml) | EN: Use a vision LLM (Gemini, GPT-4o, Claude, Ollama) to classify motion: "person", "vehicle", "package", "pet". Includes 4 sub-examples — smart push, package detection, daily summary, visitor greeting via TTS. <br> DE: Vision-LLM (Gemini, GPT-4o, Claude, Ollama) klassifiziert Motion: "Person", "Fahrzeug", "Paket", "Tier". Enthält 4 Unterbeispiele — Smart-Push, Paket-Erkennung, Tageszusammenfassung, Besucher-Begrüßung per TTS. |

## Camera-generation matrix / Kamera-Generations-Matrix

| Switch / Schalter | Gen1 (Eyes v1, 360 v1) | Gen2 (Eyes II, Innen II) |
|---|---|---|
| `switch.bosch_<cam>_privacy_mode` | ✅ Persistent | ✅ Persistent |
| `switch.bosch_<cam>_camera_light` | ✅ Manual on/off | ✅ Manual on/off |
| `switch.bosch_<cam>_front_light` | ✅ Front spotlight | (use camera_light) |
| `switch.bosch_<cam>_wallwasher` | ✅ Top+bottom LEDs | ✅ Wallwasher LEDs |
| `switch.bosch_<cam>_licht_bei_bewegung` | ❌ — use Gen1 fallbacks | ✅ Native motion-light gate |
| `switch.bosch_<cam>_motion_detection` | ⚠️ State reverts (firmware) | ⚠️ State reverts (firmware) |

EN: The "motion detection revert" is a firmware-level limitation on both generations — the on-device IVA engine overrides Cloud-API writes within ~1 s. Use the *workaround* automations above (privacy mode, downstream filtering, presence sensors) instead of fighting it.

DE: Das "Motion-Detection-Revert" ist eine Firmware-Limitierung auf beiden Generationen — die IVA-Engine auf der Kamera überschreibt Cloud-API-Writes binnen ~1 s. Stattdessen die *Workaround*-Automationen oben benutzen (Privacy-Mode, Downstream-Filter, Anwesenheits-Sensoren).

## Placeholders to replace / Zu ersetzende Platzhalter

In every file / In jeder Datei:

| Placeholder | Replace with / Ersetzen durch |
|---|---|
| `<cam>` | EN: your camera's slug (e.g. `terrasse`, `porch`, `eingang`) / DE: euer Kamera-Slug |
| `<outdoor_cam>` / `<indoor_cam>` | EN: outdoor / indoor camera slug / DE: Außen-/Innen-Kamera-Slug |
| `<doorbell_cam>` | EN: front-door camera slug / DE: Haustür-Kamera-Slug |
| `binary_sensor.porch_door` | EN: your door / window sensor entity / DE: euer Tür-/Fenster-Sensor |
| `binary_sensor.porch_presence` | EN: your presence sensor entity / DE: euer Anwesenheits-Sensor |
| `switch.porch_camera_plug` | EN: your smart plug entity / DE: eure Smart-Plug-Entity |
| `notify.mobile_app_<your_phone>` | EN: HA Companion notify service / DE: HA-Companion-Notify-Service |
| `person.thomas`, `person.partner`, … | EN: your household member entities / DE: eure Haushalts-Mitglieder |
| `your_provider_id` (AI files only) | EN: LLM Vision provider ID / DE: LLM-Vision-Provider-ID |

## Combining patterns / Patterns kombinieren

EN: Nothing prevents you from running several of these at once. A typical combination is **helper switch** + **door sensor** + **presence sensor** all feeding into the same motion-light suppression. Turning an already-off switch off is a no-op.

DE: Mehrere gleichzeitig ist kein Problem. Typische Kombination: **Helper-Switch** + **Tür-Sensor** + **Anwesenheits-Sensor** speisen alle dieselbe Bewegungslicht-Unterdrückung. Einen schon ausgeschalteten Switch nochmal auszuschalten ist ein No-Op.

## Contributing / Beitragen

EN: Have a creative automation idea that's specific to Bosch cameras? Open a PR — happy to add more examples here.

DE: Habt ihr eine kreative Automation-Idee speziell für Bosch-Kameras? PR aufmachen — gerne mehr Beispiele hier.
