import os
from typing import Annotated
from pydantic import Field
from fastmcp import FastMCP
from fastmcp.utilities.types import Image
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
                instances = await api.v1_get_instances()  # type: ignore[misc]

                devices = []
                for instance in instances:  # type: ignore[misc]
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

    @mcp.resource("corellium://devices/{instance_id}/hooks")
    async def list_device_hooks(instance_id: str) -> dict:
        """
        List all hypervisor hooks for a specific device instance.
        """
        try:
            async with corellium_api.ApiClient(configuration) as api_client:
                api = corellium_api.CorelliumApi(api_client)
                hooks = await api.v1_get_hooks(instance_id)  # type: ignore[misc]

                hooks_list = []
                for hook in hooks:  # type: ignore[misc]
                    hook_info = {
                        "identifier": getattr(hook, 'identifier', None),
                        "label": getattr(hook, 'label', None),
                        "address": getattr(hook, 'address', None),
                        "patch": getattr(hook, 'patch', None),
                        "patch_type": getattr(hook, 'patch_type', None),
                        "enabled": getattr(hook, 'enabled', None),
                        "created_at": getattr(hook, 'created_at', None),
                        "updated_at": getattr(hook, 'updated_at', None),
                        "instance_id": getattr(hook, 'instance_id', None),
                    }
                    hooks_list.append(hook_info)

                return {"hooks": hooks_list, "count": len(hooks_list)}
        except Exception as e:
            return {"error": str(e), "hooks": [], "count": 0}

    @mcp.tool
    async def take_device_screenshot(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) of the device to screenshot")],
        format: Annotated[str, Field(description="Image format: 'png' or 'jpeg'")] = "png",
        scale: Annotated[float, Field(description="Screenshot scale 1:N")] = 1.0
    ) -> Image:
        """
        Take a screenshot of a Corellium device and return it as an image.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            # API returns a file path to a temporary file
            screenshot_path = await api.v1_get_instance_screenshot(instance_id, format, scale=scale)  # type: ignore[misc]

            # Ensure we have a valid file path string
            if not isinstance(screenshot_path, str):
                raise ValueError(f"Expected file path string, got {type(screenshot_path)}")

            # Read the image data from the temporary file
            with open(screenshot_path, 'rb') as f:
                image_bytes = f.read()

            return Image(data=image_bytes, format=format)

    @mcp.tool
    async def create_hypervisor_hook(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) to create hook on")],
        label: Annotated[str, Field(description="Human-readable label for the hook")],
        address: Annotated[str, Field(description="Memory address to hook (hex string)")],
        patch: Annotated[str, Field(description="Patch code to apply")],
        patch_type: Annotated[str, Field(description="Patch type: 'csmfcc' or 'csmfvm'")]
    ) -> dict:
        """
        Create a new hypervisor hook for a Corellium device instance.
        Hooks allow intercepting execution at specific memory addresses.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)

            # Create the hook parameters
            hook_params = corellium_api.V1CreateHookParameters(
                label=label,
                address=address,
                patch=patch,
                patch_type=patch_type
            )

            # Create the hook
            hook = await api.v1_create_hook(instance_id, hook_params)  # type: ignore[misc]

            return {
                "identifier": getattr(hook, 'identifier', None),
                "label": getattr(hook, 'label', None),
                "address": getattr(hook, 'address', None),
                "patch": getattr(hook, 'patch', None),
                "patch_type": getattr(hook, 'patch_type', None),
                "enabled": getattr(hook, 'enabled', None),
                "created_at": getattr(hook, 'created_at', None),
                "updated_at": getattr(hook, 'updated_at', None),
                "instance_id": getattr(hook, 'instance_id', None),
            }

    @mcp.tool
    async def get_hypervisor_hook(
        hook_id: Annotated[str, Field(description="Hook ID to retrieve")]
    ) -> dict:
        """
        Get details of a specific hypervisor hook by its ID.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            hook = await api.v1_get_hook_by_id(hook_id)  # type: ignore[misc]

            return {
                "identifier": getattr(hook, 'identifier', None),
                "label": getattr(hook, 'label', None),
                "address": getattr(hook, 'address', None),
                "patch": getattr(hook, 'patch', None),
                "patch_type": getattr(hook, 'patch_type', None),
                "enabled": getattr(hook, 'enabled', None),
                "created_at": getattr(hook, 'created_at', None),
                "updated_at": getattr(hook, 'updated_at', None),
                "instance_id": getattr(hook, 'instance_id', None),
            }

    @mcp.tool
    async def update_hypervisor_hook(
        hook_id: Annotated[str, Field(description="Hook ID to update")],
        label: Annotated[str, Field(description="Updated label for the hook")],
        address: Annotated[str, Field(description="Updated memory address (hex string)")],
        patch: Annotated[str, Field(description="Updated patch code")],
        patch_type: Annotated[str, Field(description="Updated patch type: 'csmfcc' or 'csmfvm'")]
    ) -> dict:
        """
        Update an existing hypervisor hook.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)

            # Create the hook parameters
            hook_params = corellium_api.V1CreateHookParameters(
                label=label,
                address=address,
                patch=patch,
                patch_type=patch_type
            )

            # Update the hook
            hook = await api.v1_update_hook(hook_id, hook_params)  # type: ignore[misc]

            return {
                "identifier": getattr(hook, 'identifier', None),
                "label": getattr(hook, 'label', None),
                "address": getattr(hook, 'address', None),
                "patch": getattr(hook, 'patch', None),
                "patch_type": getattr(hook, 'patch_type', None),
                "enabled": getattr(hook, 'enabled', None),
                "created_at": getattr(hook, 'created_at', None),
                "updated_at": getattr(hook, 'updated_at', None),
                "instance_id": getattr(hook, 'instance_id', None),
            }

    @mcp.tool
    async def delete_hypervisor_hook(
        hook_id: Annotated[str, Field(description="Hook ID to delete")]
    ) -> dict:
        """
        Delete a hypervisor hook.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            await api.v1_delete_hook(hook_id)  # type: ignore[misc]

            return {"success": True, "hook_id": hook_id, "message": "Hook deleted successfully"}

    @mcp.tool
    async def execute_hypervisor_hooks(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) to execute hooks on")]
    ) -> dict:
        """
        Execute all hypervisor hooks on a device instance.
        This activates all configured hooks.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            await api.v1_execute_hyper_trace_hooks(instance_id)  # type: ignore[misc]

            return {"success": True, "instance_id": instance_id, "message": "Hooks executed successfully"}

    @mcp.tool
    async def clear_hypervisor_hooks(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) to clear hooks on")]
    ) -> dict:
        """
        Clear/disable all hypervisor hooks on a device instance.
        This deactivates all hooks without deleting them.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            await api.v1_clear_hyper_trace_hooks(instance_id)  # type: ignore[misc]

            return {"success": True, "instance_id": instance_id, "message": "Hooks cleared successfully"}

    @mcp.tool
    async def get_instance_services_ip(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) to get services IP for")]
    ) -> str:
        """
        Get the services IP address for a Corellium device instance.
        The services IP is used to access device services like SSH, HTTP, etc.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            instance = await api.v1_get_instance(instance_id)  # type: ignore[misc]

            return getattr(instance, 'service_ip', None) or ""

    @mcp.tool
    async def get_device_ip(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) to get IP for")]
    ) -> str:
        """
        Get the device IP address for a Corellium instance.
        Returns just the IP address string.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            instance = await api.v1_get_instance(instance_id)  # type: ignore[misc]

            return getattr(instance, 'wifi_ip', None) or ""

    @mcp.tool
    async def get_instance_console_log(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) to get console log for")]
    ) -> str:
        """
        Get the console log for a Corellium device instance.
        Returns the current console log output as a string.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            console_log = await api.v1_get_instance_console_log(instance_id)  # type: ignore[misc]

            # API returns str according to docs
            return str(console_log) if console_log else ""

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
