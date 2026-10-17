# ai-computer-brain

The shared memory that keeps every AI agent in your company in sync: a self-hosted MCP server with a Hermes-style console. This npm package is a launcher for Node users: it installs the Python package with `uv` (installing `uv` and Python 3.12 if needed) and runs the `brain` console.

```bash
npm install -g ai-computer-brain
brain
```

or without installing anything globally:

```bash
npx ai-computer-brain
```

`brain --update` reinstalls the latest version. Everything else, from connecting agents to putting the brain on your own domain, is documented at https://github.com/Cognio-so/AI_computer_brain.
