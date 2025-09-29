import os
from typing import Annotated
from pydantic import Field
from fastmcp import FastMCP
import corellium_api


def create_server() -> FastMCP:
    mcp = FastMCP("corellium", stateless_http=True)

    corellium_api_token = os.getenv("CORELLIUM_API_TOKEN")
    if not corellium_api_token:
        raise ValueError("CORELLIUM_API_TOKEN environment variable is required")

    corellium_api_host = os.getenv("CORELLIUM_API_HOST", "https://app.corellium.com/api")

    configuration = corellium_api.Configuration(
        host=corellium_api_host
    )
    configuration.access_token = corellium_api_token

    @mcp.tool
    async def hello_world(
        name: Annotated[str, Field(description="Name to greet", examples=["World", "Alice", "Bob"])] = "World"
    ) -> str:
        """
        A simple hello world tool that greets the provided name.
        """
        return f"Hello, {name}!"

    @mcp.resource("corellium://devices")
    async def list_devices() -> dict:
        """
        List all devices on the Corellium server.
        """
        try:
            async with corellium_api.ApiClient(configuration) as api_client:
                api = corellium_api.CorelliumApi(api_client)
                instances = await api.v1_get_instances()

                devices = []
                for instance in instances:
                    state_value = None
                    if hasattr(instance, 'state') and instance.state and hasattr(instance.state, 'state'):
                        state_value = instance.state.state

                    created_value = None
                    if hasattr(instance, 'created') and instance.created:
                        created_value = instance.created.isoformat()

                    device_info = {
                        "id": getattr(instance, 'id', None),
                        "name": getattr(instance, 'name', None),
                        "model": getattr(instance, 'model', None),
                        "os": getattr(instance, 'os', None),
                        "state": state_value,
                        "flavor": getattr(instance, 'flavor', None),
                        "created": created_value,
                        "project": getattr(instance, 'project', None),
                    }
                    devices.append(device_info)

                return {"devices": devices, "count": len(devices)}
        except Exception as e:
            return {"error": str(e), "devices": [], "count": 0}

    return mcp


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Corellium MCP Server")
    parser.add_argument("--http", action="store_true", help="Use HTTP transport instead of stdio")
    parser.add_argument("--host", default="127.0.0.1", help="Host for HTTP transport (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="Port for HTTP transport (default: 8000)")
    parser.add_argument("--path", default="/mcp", help="Path for HTTP transport (default: /mcp)")

    args = parser.parse_args()

    mcp = create_server()

    if args.http:
        mcp.run(
            transport="http",
            host=args.host,
            port=args.port,
            path=args.path
        )
    else:
        mcp.run()


if __name__ == "__main__":
    main()
