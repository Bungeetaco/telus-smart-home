# TELUS Smart Home for Home Assistant: Documentation

This project brings a **TELUS Smart Home** system (a white-labeled Alarm.com
deployment) into **Home Assistant**. TELUS ships no official API, so the project
is built by reverse-engineering the browser and mobile-app traffic of a real
account. It has two independent halves:

- a Home Assistant **custom integration** (`telus_smarthome_alarmdotcom`) for panel, lock,
  sensor, event, and camera entities, and
- a standalone **camera bridge** container that republishes Alarm.com's WebRTC
  video as plain RTSP and exposes a small loopback control API.

The docs below describe the software generically; any operator running this
project can follow them. Placeholders such as `<ha-host>`, `<ha-host-ip>`,
`<doorbell-ip>`, `<unit-id>`, and `<did>` stand in for values specific to your
own deployment.

## Overview

| Doc | What it covers |
| --- | --- |
| [architecture.md](architecture.md) | The big picture: the two components, the two Alarm.com backends, and the end-to-end data/control flows. Start here. |

## Components

| Doc | What it covers |
| --- | --- |
| [ha-integration.md](ha-integration.md) | The `telus_smarthome_alarmdotcom` Home Assistant hub integration: entities, services, coordinators, and how it consumes the bridge. |
| [camera-bridge.md](camera-bridge.md) | The standalone Docker service that pulls WebRTC video and republishes it as local RTSP. |
| [control-api.md](control-api.md) | The bridge's loopback HTTP control server: state polling, snapshots, clips, talkdown, camera settings/diagnostics/siren/privacy, and the credential handoff. |

## Reference

| Doc | What it covers |
| --- | --- |
| [alarm-com-api.md](alarm-com-api.md) | The Alarm.com backend API and the TELUS SSO login handshake (both reimplementations). |
| [mobile-api-findings.md](mobile-api-findings.md) | Survey of mobile-app API actions this project does NOT use, and what each would enable. |
| [web-portal-api-findings.md](web-portal-api-findings.md) | Survey of the web portal API surface (from a browser HAR): camera settings including speaker volume, diagnostics, and other writes. |
| [configuration.md](configuration.md) | Every configurable knob across the bridge and the integration, in one place. |
| [backlog.md](backlog.md) | The enhancement backlog: every open, blocked, and settled improvement idea across the project, with stable `E<n>` IDs and the evidence behind each status. |

## Guides

| Doc | What it covers |
| --- | --- |
| [setup.md](setup.md) | Installation and deployment from nothing to working entities, including host-networking requirements. |
| [troubleshooting.md](troubleshooting.md) | Known issues and gotchas as symptom → cause → fix, across both components. |
| [development.md](development.md) | Contributor notes: repo layout, versioning & the release process, and building locally. |

## Talkdown (doorbell two-way audio)

| Doc | What it covers |
| --- | --- |
| [talkdown-fullduplex-protocol.md](talkdown-fullduplex-protocol.md) | Implementation-grade reference for the ADC-VDB750 full-duplex HTTP-PUT talk path, confirmed against a real capture. |
| [talkdown-DEPLOY.md](talkdown-DEPLOY.md) | Manual, human-run steps to enable and validate talkdown against a live doorbell. |
| [talkdown-automation.yaml](talkdown-automation.yaml) | A reference Home Assistant automation (a Frigate-zone dwell deterrent) that drives the talkdown service. |
