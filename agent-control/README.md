# agent-control

The control-plane app. Provisions, configures, and monitors agents
running on the host.

## Status

Not implemented in this repo yet. A prototype lives at
`/home/alice/agent-control/` (legacy prototype). Decision pending
on whether to lift-and-shift or rewrite cleanly.

## Surface (planned)

```sh
sudo agent-control create <name> --tools t1,t2,...    # provision agent
sudo agent-control delete <name>                      # tear down
sudo agent-control list                               # list agents + status
sudo agent-control enable-tool <agent> <tool>         # grant tool access
sudo agent-control disable-tool <agent> <tool>        # revoke
sudo agent-control logs <agent>                       # tail journal
```

`create` is responsible for:
1. Creating the Linux user + home dir
2. Rendering `agent.toml`, `.claude.json`, `settings.json` from `/opt/agents/templates/`
3. Setting up `LoadCredentialEncrypted=` references for the agent's enabled tools
4. Enabling the templated systemd unit `agent@<name>.service`
