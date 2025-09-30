import os
import json
from typing import Annotated
from contextlib import asynccontextmanager
from pydantic import Field
from fastmcp import FastMCP, Context
from fastmcp.utilities.types import Image
from fastmcp.resources import TextResource
import corellium_api
import anyio


# Configuration
REFRESH_INTERVAL = int(os.getenv("CORELLIUM_REFRESH_INTERVAL", "60"))


def create_server() -> FastMCP:
    corellium_api_token = os.getenv("CORELLIUM_API_TOKEN")
    if not corellium_api_token:
        raise ValueError("CORELLIUM_API_TOKEN environment variable is required")

    corellium_api_host = os.getenv("CORELLIUM_API_HOST", "https://app.corellium.com/api")

    configuration = corellium_api.Configuration(
        host=corellium_api_host
    )
    configuration.access_token = corellium_api_token

    # Track current device resources
    current_device_ids: set[str] = set()

    async def refresh_device_resources(mcp: FastMCP) -> None:
        """Fetch devices from Corellium and create/update resources."""
        nonlocal current_device_ids

        try:
            async with corellium_api.ApiClient(configuration) as api_client:
                api = corellium_api.CorelliumApi(api_client)
                instances = await api.v1_get_instances()  # type: ignore[misc]

                new_device_ids: set[str] = set()

                for instance in instances:  # type: ignore[misc]
                    instance_id = getattr(instance, 'id', None)
                    if not instance_id:
                        continue

                    new_device_ids.add(instance_id)

                    # Convert instance to dict for JSON serialization
                    try:
                        instance_dict = instance.to_dict()  # type: ignore[union-attr]
                    except (AttributeError, TypeError):
                        # Fallback to manual serialization
                        instance_dict = {k: v for k, v in instance.__dict__.items() if not k.startswith('_')}  # type: ignore[union-attr]

                    # Extract info for description
                    device_name = getattr(instance, 'name', instance_id)
                    flavor = getattr(instance, 'flavor', 'Unknown')
                    os_version = getattr(instance, 'os', 'Unknown')

                    # Create/update resource for this device
                    resource_uri = f"device://{instance_id}"

                    resource = TextResource(
                        uri=resource_uri,  # type: ignore[arg-type]
                        name=f"Device: {device_name}",
                        text=json.dumps(instance_dict, indent=2, default=str),
                        description=f"Corellium device {device_name} - {flavor} {os_version} ({instance_id})",
                        mime_type="application/json"
                    )
                    mcp.add_resource(resource)

                # Remove resources for devices that no longer exist
                removed_ids = current_device_ids - new_device_ids
                for removed_id in removed_ids:
                    resource_uri = f"device://device/{removed_id}"
                    # Note: FastMCP doesn't have a remove_resource method, but replacing is fine
                    # Resources will be overwritten on next add_resource call

                # Update tracking
                has_changes = current_device_ids != new_device_ids
                current_device_ids = new_device_ids

                # Notify clients of changes if any
                if has_changes:
                    try:
                        from fastmcp.server.dependencies import get_context
                        ctx = get_context()
                        await ctx.send_resource_list_changed()
                    except RuntimeError:
                        # No context available (e.g., during startup)
                        pass

        except Exception as e:
            # Log error but don't crash the background task
            print(f"Error refreshing device resources: {e}")

    @asynccontextmanager
    async def lifespan(mcp: FastMCP):
        """Lifespan handler to run background refresh task."""
        # Do initial refresh
        await refresh_device_resources(mcp)

        # Start background refresh task
        async with anyio.create_task_group() as tg:
            async def refresh_loop():
                while True:
                    await anyio.sleep(REFRESH_INTERVAL)
                    await refresh_device_resources(mcp)

            tg.start_soon(refresh_loop)

            try:
                yield
            finally:
                tg.cancel_scope.cancel()

    mcp = FastMCP("corellium", stateless_http=True, lifespan=lifespan)

    @mcp.tool
    async def hello_world(
        name: Annotated[str, Field(description="Name to greet", examples=["World", "Alice", "Bob"])] = "World"
    ) -> str:
        """
        A simple hello world tool that greets the provided name.
        """
        return f"Hello, {name}!"

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
    ) -> None:
        """
        Delete a hypervisor hook.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            await api.v1_delete_hook(hook_id)  # type: ignore[misc]

    @mcp.tool
    async def execute_hypervisor_hooks(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) to execute hooks on")]
    ) -> None:
        """
        Execute all hypervisor hooks on a device instance.
        This activates all configured hooks.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            await api.v1_execute_hyper_trace_hooks(instance_id)  # type: ignore[misc]

    @mcp.tool
    async def clear_hypervisor_hooks(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) to clear hooks on")]
    ) -> None:
        """
        Clear/disable all hypervisor hooks on a device instance.
        This deactivates all hooks without deleting them.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            await api.v1_clear_hyper_trace_hooks(instance_id)  # type: ignore[misc]

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
        instance_id: Annotated[str, Field(description="Instance ID (UUID) to get console log for")],
        filepath: Annotated[str, Field(description="File path to write the console log to")]
    ) -> int:
        """
        Get the console log for a Corellium device instance and write it to a file.
        Returns the number of bytes written to the file.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            console_log = await api.v1_get_instance_console_log(instance_id)  # type: ignore[misc]

            # Convert to string and encode to bytes
            log_content = str(console_log) if console_log else ""
            log_bytes = log_content.encode('utf-8')

            # Write to file
            with open(filepath, 'wb') as f:
                bytes_written = f.write(log_bytes)

            return bytes_written

    @mcp.tool
    async def download_kernel_binary(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) to authorize the request")],
        model: Annotated[str, Field(description="Device model", examples=["iPhone8,1"])],
        ios_version: Annotated[str, Field(description="iOS version", examples=["19A404"])],
        filepath: Annotated[str, Field(description="File path to write the kernel Mach-O binary to")]
    ) -> int:
        """
        Download a kernel binary for a specific iOS device model and version.
        This tool is only available for iOS devices.
        Returns the number of bytes written to the file.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            # Step 1: Get authorization token
            auth_url = f"{configuration.host}/v1/instances/{instance_id}/kernel-authorize"
            headers = {
                "Authorization": f"Bearer {configuration.access_token}",  # type: ignore[attr-defined]
                "Accept": "*/*"
            }

            auth_response = await api_client.rest_client.request(
                method="POST",
                url=auth_url,
                headers=headers
            )

            if auth_response.status != 200:
                raise Exception(f"Failed to authorize kernel download: {auth_response.status} {auth_response.reason}")

            # Parse the token from response
            auth_data = json.loads(auth_response.data.decode('utf-8'))  # type: ignore[attr-defined]
            token = auth_data.get("token")
            if not token:
                raise Exception("No token received from authorization endpoint")

            # Step 2: Download the kernel binary
            kernel_url = f"{configuration.host}/v1/preauthed/{token}/kernel-{model}-{ios_version}"

            kernel_response = await api_client.rest_client.request(
                method="GET",
                url=kernel_url,
                headers={"Accept": "*/*"}
            )

            if kernel_response.status != 200:
                raise Exception(f"Failed to download kernel binary: {kernel_response.status} {kernel_response.reason}")

            # Write binary data to file
            kernel_data = kernel_response.data  # type: ignore[attr-defined]
            with open(filepath, 'wb') as f:
                bytes_written = f.write(kernel_data)

            return bytes_written

    @mcp.tool
    async def type_text(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) of the device")],
        text: Annotated[str, Field(description="Text to type into the device")]
    ) -> None:
        """
        Type text into a Corellium device instance.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)

            input_obj = {
                "text": text
            }

            # Send input to device
            response = await api.v1_post_instance_input(instance_id, [input_obj])  # type: ignore[misc]

    @mcp.tool
    async def press_button(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) of the device")],
        buttons: Annotated[list[str], Field(description="List of button names to press")]
    ) -> None:
        """
        Press device buttons on a Corellium device.

        IMPORTANT: All buttons in the list will be pressed simultaneously. For duplicate key presses
        or sequential button presses, call this tool multiple times.

        This tool should be used to control the device, as Full Keyboard Access is enabled by default
        on all Corellium devices, allowing navigation and interaction through button presses. Use space to activate the selected item.

        Available VM buttons: homeButton, holdButton, volumeUp, volumeDown, ringerSwitch, backButton, overviewButton

        Available Keyboard buttons: again, alt, alterase, apostrophe, back, backslash, backspace,
        bassboost, bookmarks, bsp, calc, camera, cancel, caps, capslock, chat, close, closecd, comma,
        compose, computer, config, connect, copy, ctrl, cut, cyclewindows, dashboard, del, delete,
        deletefile, dot, down, edit, eject, ejectclose, email, end, enter, equal, esc, escape, exit,
        f1, f10, f11, f12, f13, f14, f15, f16, f17, f18, f19, f2, f20, f21, f22, f23, f24, f3, f4, f5,
        f6, f7, f8, f9, fastfwd, file, finance, find, forward, front, grave, hangeul, hanja, help,
        henkan, home, homepage, hp, hrgn, ins, insert, iso, k102, kp0, kp1, kp2, kp3, kp4, kp5, kp6,
        kp7, kp8, kp9, kpasterisk, kpcomma, kpdot, kpenter, kpequal, kpjpcomma, kpleftparen, kpminus,
        kpplus, kpplusminus, kprightparen, kpslash, ktkn, ktknhrgn, left, leftalt, leftbrace, leftctrl,
        leftmeta, leftshift, linefeed, macro, mail, menu, meta, minus, move, msdos, muhenkan, mute, new,
        next, numlock, open, pagedown, pageup, paste, pause, pausecd, pgdn, pgup, phone, play, playcd,
        playpause, power, previous, print, prog1, prog2, prog3, prog4, props, question, record, redo,
        refresh, return, rewind, right, rightalt, rightbrace, rightctrl, rightmeta, rightshift, ro,
        rotate, scale, screenlock, scrolldown, scrolllock, scrollup, search, semicolon, sendfile, setup,
        shift, shop, slash, sleep, sound, space, sport, stop, stopcd, suspend, sysrq, tab, undo, up,
        voldown, volup, wakeup, www, xfer, yen, zkhk
        """
        # Check for duplicate buttons
        if len(buttons) != len(set(buttons)):
            duplicates = [btn for btn in set(buttons) if buttons.count(btn) > 1]
            raise ValueError(f"Duplicate buttons not allowed. Found duplicates: {', '.join(duplicates)}")

        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)

            # Create button press input with empty position
            input_obj = corellium_api.TouchInput(
                position=[],
                buttons=buttons
            )

            # Send input to device
            response = await api.v1_post_instance_input(instance_id, [input_obj])  # type: ignore[misc]

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
