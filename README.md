# Corellium MCP Server

A simple MCP server for Corellium with a hello_world tool.

## Setup

### 1. Create Virtual Environment

```bash
python3.12 -m venv venv/
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

### 2. Install Dependencies

```bash
pip install -e .
```

## Usage

Run with stdio transport (default):
```bash
corellium-mcp
```

Run with HTTP transport:
```bash
corellium-mcp --http --host 127.0.0.1 --port 8000 --path /mcp
```

```bash
npx @modelcontextprotocol/inspector --cli corellium-mcp
```

## Tools

- `hello_world`: A simple greeting tool that takes a name parameter and returns a greeting message.
