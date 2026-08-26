# Torrent detection

There are two sources, and they carry different weight.

**The Xray routing tag.** A rule with `"protocol": ["bittorrent"]` puts a `TORRENT` tag on the connection, the agent sees it in the log and sends an event. It works with no extras, but catches little: Xray recognises BitTorrent by the plaintext handshake at the start of a connection, while modern clients encrypt the stream by default (MSE/PE) and live in DHT and uTP over UDP. The handshake usually never happens, so mostly old clients with encryption disabled get caught.

**nDPI verdicts.** [nDPI](https://github.com/ntop/nDPI) has heuristics for encrypted BitTorrent, DHT packets and uTP. This is traffic inspection rather than reading log metadata, so it sees considerably more.

## Turning it on

The **Torrent detection via nDPI** toggle: **Settings → Violations**.

Nothing else is required — no packages to install on the node, no `.env` to edit. The `nDPId` daemon ships inside the agent image, so enabling it comes down to starting a pair of processes. The agent can listen on the interface because it already runs in host networking with `privileged` — without that, network metrics would not work either. The interface is picked from the default route rather than every interface at once: there is no point inspecting the traffic of the agent itself.

The toggle state is pushed to every connected agent immediately, and re-sent to an agent when it connects — a node restart does not lose the setting.

::: tip Safe detection requires Agent 1.7.3 or newer
The daemon first shipped in 1.5.0, but versions 1.7.1–1.7.3 close important false-positive paths: they no longer attribute a shared destination to every user, ignore weak nDPI guesses, and filter service/web-port noise. Admin 4.6.2 therefore expects Agent 1.7.3. An older agent still reports its version honestly, but does not provide the full set of protections.
:::

## How a verdict becomes a violation

nDPI sees traffic after NAT, on behalf of the node, and knows nothing about whose client it is. The destination address, however, is the same for it and for Xray — that is what the agent uses to tie a verdict to the log line that does have a user in it.

The event carries a `detected_by` field:

| Value | Meaning |
|-------|---------|
| `xray_routing` | the Xray routing tag fired |
| `ndpi` | traffic inspection verdict |

When somebody asks why they were blocked, this field says what exactly caught them. Events recorded before the second source existed are marked `xray_routing` — there was nothing else.

## Fine-tuning

Rarely needed, the toggle covers the normal case:

```ini
AGENT_NDPI_ENABLED=true          # start without waiting for the panel command
AGENT_NDPI_MANAGE_DAEMON=true    # false if the daemon is already running on the node
AGENT_NDPI_INTERFACE=eth0        # empty means the default-route interface
AGENT_NDPI_SOCKET_PATH=/tmp/ndpid-distributor.sock
AGENT_NDPI_WINDOW_SECONDS=120    # how long a verdict stays fresh
```

If the socket at that path already answers, the agent assumes the daemon was started externally and does not spawn its own.

## Worth thinking about first

::: warning This is traffic inspection
Reading the Xray log means metadata the node already writes down. nDPI inspects traffic on the interface. That is a noticeably bigger step, and it deserves a deliberate decision.
:::

The load is modest: the daemon works from heuristics over the first packets of a flow, and the agent tells it not to send packet events at all — they carry no verdict and only loaded the socket.
