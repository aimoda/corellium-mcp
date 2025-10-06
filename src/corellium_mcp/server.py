import os
import json
import socket
import select
import threading
from typing import Annotated, Any
from contextlib import asynccontextmanager
from pydantic import Field
from fastmcp import FastMCP, Context
from fastmcp.utilities.types import Image
from fastmcp.resources import TextResource
import corellium_api
import anyio
import paramiko
import socketserver


# Configuration
REFRESH_INTERVAL = int(os.getenv("CORELLIUM_REFRESH_INTERVAL", "60"))


# Port forwarding infrastructure
class ForwardServer(socketserver.ThreadingTCPServer):  # type: ignore[misc]
    daemon_threads = True
    allow_reuse_address = True


class Handler(socketserver.BaseRequestHandler):  # type: ignore[misc]
    def handle(self) -> None:
        try:
            chan = self.ssh_transport.open_channel(  # type: ignore[attr-defined]
                "direct-tcpip",
                (self.chain_host, self.chain_port),  # type: ignore[attr-defined]
                self.request.getpeername(),
            )
        except Exception as e:
            print(f"Incoming request to {self.chain_host}:{self.chain_port} failed: {e}")  # type: ignore[attr-defined]
            return
        if chan is None:
            print(f"Incoming request to {self.chain_host}:{self.chain_port} was rejected")  # type: ignore[attr-defined]
            return

        while True:
            r, w, x = select.select([self.request, chan], [], [])
            if self.request in r:
                data = self.request.recv(1024)
                if len(data) == 0:
                    break
                chan.send(data)
            if chan in r:
                data = chan.recv(1024)
                if len(data) == 0:
                    break
                self.request.send(data)

        chan.close()
        self.request.close()


def create_server() -> FastMCP:
    corellium_api_token = os.getenv("CORELLIUM_API_TOKEN")
    if not corellium_api_token:
        raise ValueError("CORELLIUM_API_TOKEN environment variable is required")

    corellium_api_host = os.getenv("CORELLIUM_API_HOST", "https://app.corellium.com/api")
    corellium_project_id = os.getenv("CORELLIUM_PROJECT_ID")

    configuration = corellium_api.Configuration(
        host=corellium_api_host
    )
    configuration.access_token = corellium_api_token

    # Disable client-side validation globally to work around API validation bugs
    # (e.g., ProjectSettings.dhcp can be None in API responses but validation rejects it)
    configuration.client_side_validation = False
    corellium_api.Configuration.set_default(configuration)

    # Track current device resources
    current_device_ids: set[str] = set()

    # Track current port forward resource URIs
    current_port_forward_uris: set[str] = set()

    # SSH key for port forwarding (generated at startup)
    ssh_key: paramiko.RSAKey | None = None

    # Track active port forwards: (instance_id, local_port) -> (transport, server_thread, server)
    active_port_forwards: dict[tuple[str, int], tuple[paramiko.Transport, threading.Thread, ForwardServer]] = {}

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

    async def monitor_port_forwards(mcp: FastMCP) -> None:
        """Monitor active port forwards and clean up dead connections."""
        nonlocal active_port_forwards

        while True:
            await anyio.sleep(5)  # Check every 5 seconds

            dead_forwards: list[tuple[str, int]] = []

            for forward_key, (transport, _, server) in list(active_port_forwards.items()):
                # Check if transport is still active
                if not transport.is_active():
                    dead_forwards.append(forward_key)

            # Clean up dead forwards
            for forward_key in dead_forwards:
                instance_id, local_port = forward_key
                print(f"Port forward disconnected: {instance_id} on port {local_port}")

                # Remove from tracking and shutdown server
                if forward_key in active_port_forwards:
                    _, _, server = active_port_forwards[forward_key]
                    try:
                        server.shutdown()
                    except Exception as e:
                        print(f"Error shutting down ForwardServer: {e}")
                    del active_port_forwards[forward_key]

            # Refresh resources and notify client if any were removed
            if dead_forwards:
                await refresh_port_forward_resources(mcp)
                try:
                    from fastmcp.server.dependencies import get_context
                    ctx = get_context()
                    await ctx.send_resource_list_changed()
                except RuntimeError:
                    # No context available
                    pass

    @asynccontextmanager
    async def lifespan(mcp: FastMCP):
        """Lifespan handler to run background refresh task."""
        nonlocal ssh_key

        # Generate SSH key for port forwarding
        print("Generating SSH key for port forwarding...")
        ssh_key = paramiko.RSAKey.generate(bits=2048)

        # Do initial refresh
        await refresh_device_resources(mcp)

        # Start background refresh task
        async with anyio.create_task_group() as tg:
            async def refresh_loop():
                while True:
                    await anyio.sleep(REFRESH_INTERVAL)
                    await refresh_device_resources(mcp)

            tg.start_soon(refresh_loop)
            tg.start_soon(monitor_port_forwards, mcp)

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

    @mcp.tool
    async def get_supported_models() -> list[dict]:
        """
        Get list of all supported device models available in Corellium.

        Returns information about each model including type, name, flavor, and hardware details.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            models = await api.v1_get_models()  # type: ignore[misc]

            return [model.to_dict() for model in models]  # type: ignore[misc, union-attr]

    @mcp.tool
    async def get_model_software(
        model: Annotated[str, Field(description="Device model identifier", examples=["iPhone8,1", "iPhone14,2"])],
        limit: Annotated[int, Field(ge=1, le=100, description="Number of firmware versions to return (default: 20)")] = 20,
        offset: Annotated[int, Field(ge=0, description="Number of firmware versions to skip (default: 0)")] = 0
    ) -> dict:
        """
        Get available firmware/software versions for a specific device model.

        Returns paginated firmware versions with details including version,
        build ID, checksums, size, and download URLs.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            # Make raw API call to avoid deserialization issues with empty datetime fields
            url = f"{configuration.host}/v1/models/{model}/software"
            headers = {
                "Authorization": f"Bearer {configuration.access_token}",  # type: ignore[attr-defined]
                "Accept": "application/json"
            }

            response = await api_client.rest_client.request(
                method="GET",
                url=url,
                headers=headers
            )

            if response.status != 200:
                raise Exception(f"Failed to get model software: {response.status} {response.reason}")

            # Parse JSON response
            firmwares_data = json.loads(response.data.decode('utf-8'))  # type: ignore[attr-defined]
            all_firmwares = firmwares_data if isinstance(firmwares_data, list) else []

            # Apply pagination
            total = len(all_firmwares)
            paginated_firmwares = all_firmwares[offset:offset + limit]

            return {
                "firmwares": paginated_firmwares,
                "total": total,
                "limit": limit,
                "offset": offset,
                "has_more": offset + limit < total
            }

    @mcp.tool
    async def create_instance(
        name: Annotated[str, Field(description="Instance name")],
        flavor: Annotated[str, Field(description="Device flavor/type", examples=["ranchu", "iphone6"])],
        os: Annotated[str, Field(description="OS version", examples=["13.0", "17.0"])],
        osbuild: Annotated[str, Field(description="OS build version", examples=["17A577", "19A404"])],
        project_id: Annotated[str | None, Field(description="Project UUID (optional, uses default project if not provided)")] = None,
        jailbroken: Annotated[bool, Field(description="Whether to jailbreak the device (default: True)")] = True,
    ) -> dict:
        """
        Create a new Corellium device instance.

        If project_id is not provided, the CORELLIUM_PROJECT_ID environment variable
        will be used. If that is not set, the first available project will be used automatically.
        The jailbroken flag determines the patches applied:
        - jailbroken=True: applies ["jailbroken"] patches
        - jailbroken=False: applies ["corelliumd"] patches
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)

            # Get project_id if not provided
            if project_id is None:
                # Check environment variable first
                if corellium_project_id:
                    project_id = corellium_project_id
                else:
                    # Fall back to fetching all projects and using the first one
                    projects = await api.v1_get_projects()  # type: ignore[misc]
                    if not projects or len(projects) == 0:  # type: ignore[arg-type]
                        raise ValueError("No projects found. Please provide a project_id.")
                    project_id = getattr(projects[0], 'id', None)  # type: ignore[index]
                    if not project_id:
                        raise ValueError("Could not determine default project ID.")

            # Set patches based on jailbroken flag
            patches = ["jailbroken"] if jailbroken else ["corelliumd"]

            # Create instance options
            instance_options = corellium_api.InstanceCreateOptions(
                name=name,
                flavor=flavor,
                project=project_id,
                os=os,
                osbuild=osbuild,
                patches=patches
            )

            # Create the instance
            instance = await api.v1_create_instance(instance_options)  # type: ignore[misc]

            # Return key fields from the instance
            return {
                "id": getattr(instance, 'id', None),
                "name": getattr(instance, 'name', None),
                "flavor": getattr(instance, 'flavor', None),
                "type": getattr(instance, 'type', None),
                "project": getattr(instance, 'project', None),
                "state": getattr(instance, 'state', None),
                "model": getattr(instance, 'model', None),
                "os": getattr(instance, 'os', None),
                "patches": getattr(instance, 'patches', None),
                "service_ip": getattr(instance, 'service_ip', None),
                "wifi_ip": getattr(instance, 'wifi_ip', None),
                "created": getattr(instance, 'created', None)
            }

    @mcp.tool
    async def delete_instance(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) to delete")],
        ctx: Context | None = None
    ) -> None:
        """
        Delete a Corellium device instance.

        This permanently removes the instance.
        """
        nonlocal current_device_ids, active_port_forwards

        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            await api.v1_delete_instance(instance_id)  # type: ignore[misc]

        # Clean up any port forwards for this instance
        port_forwards_to_remove = [key for key in active_port_forwards.keys() if key[0] == instance_id]
        for forward_key in port_forwards_to_remove:
            transport, _, server = active_port_forwards[forward_key]
            try:
                server.shutdown()
            except Exception as e:
                print(f"Error shutting down ForwardServer: {e}")
            try:
                transport.close()
            except Exception as e:
                print(f"Error closing SSH transport: {e}")
            del active_port_forwards[forward_key]

        # Remove from device tracking
        current_device_ids.discard(instance_id)

        # Remove device resource
        device_resource_uri = f"device://{instance_id}"
        mcp._resource_manager._resources.pop(device_resource_uri, None)

        # Refresh port forward resources if any were removed
        if port_forwards_to_remove:
            await refresh_port_forward_resources(mcp)

        # Notify clients of changes
        if ctx:
            await ctx.send_resource_list_changed()

    @mcp.tool
    async def start_instance(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) to start")]
    ) -> None:
        """
        Start a stopped Corellium device instance.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            await api.v1_start_instance(instance_id)  # type: ignore[misc]

    @mcp.tool
    async def stop_instance(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) to stop")]
    ) -> None:
        """
        Stop a running Corellium device instance.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            await api.v1_stop_instance(instance_id)  # type: ignore[misc]

    @mcp.tool
    async def reboot_instance(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) to reboot")]
    ) -> None:
        """
        Reboot a running Corellium device instance.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            await api.v1_reboot_instance(instance_id)  # type: ignore[misc]

    @mcp.tool
    async def pause_instance(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) to pause")]
    ) -> None:
        """
        Pause a running Corellium device instance.

        Paused instances can be resumed with unpause_instance.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            await api.v1_pause_instance(instance_id)  # type: ignore[misc]

    @mcp.tool
    async def unpause_instance(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) to unpause")]
    ) -> None:
        """
        Unpause a paused Corellium device instance.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            await api.v1_unpause_instance(instance_id)  # type: ignore[misc]

    async def refresh_port_forward_resources(mcp: FastMCP) -> None:
        """Update resources for active port forwards."""
        nonlocal active_port_forwards, current_port_forward_uris

        new_port_forward_uris: set[str] = set()

        for (instance_id, local_port), (transport, _, _) in active_port_forwards.items():
            resource_uri = f"kernel-debug://{instance_id}/{local_port}"
            new_port_forward_uris.add(resource_uri)

            resource_data = {
                "instance_id": instance_id,
                "local_port": local_port,
                "remote_port": 4000,
                "local_endpoint": f"127.0.0.1:{local_port}",
                "status": "active" if transport.is_active() else "disconnected"
            }

            resource = TextResource(
                uri=resource_uri,  # type: ignore[arg-type]
                name=f"Kernel Debug Port Forward: {instance_id}:{local_port}",
                text=json.dumps(resource_data, indent=2),
                description=f"Port forwarding 127.0.0.1:{local_port} -> port 4000 for kernel debugging",
                mime_type="application/json"
            )
            mcp.add_resource(resource)

        # Remove resources for port forwards that no longer exist
        removed_uris = current_port_forward_uris - new_port_forward_uris
        for removed_uri in removed_uris:
            # Directly remove from resource manager since FastMCP doesn't have a remove_resource method
            mcp._resource_manager._resources.pop(removed_uri, None)

        # Update tracking
        current_port_forward_uris = new_port_forward_uris

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
        This tool is only available for iOS devices that are in the running (and fully booted) state.
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

    @mcp.tool
    async def execute_ssh_command(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) of the device")],
        command: Annotated[str, Field(description="Command to execute on the device")],
        timeout: Annotated[int, Field(description="Command timeout in seconds (default: 30)")] = 30,
        working_directory: Annotated[str | None, Field(description="Optional working directory to execute command in")] = None
    ) -> dict:
        """
        Execute a command on a Corellium device instance via SSH.

        Connects to the device via SSH and executes the specified command,
        returning stdout, stderr, and exit code.

        Only works on jailbroken devices.
        """
        nonlocal ssh_key

        if ssh_key is None:
            raise ValueError("SSH key not initialized. Server may still be starting up.")

        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)

            # Get instance details
            instance = await api.v1_get_instance(instance_id)  # type: ignore[misc]
            project_id = getattr(instance, 'project', None)
            service_ip = getattr(instance, 'service_ip', None)

            if not project_id:
                raise ValueError(f"Could not determine project ID for instance {instance_id}")
            if not service_ip:
                raise ValueError(f"Could not determine service IP for instance {instance_id}")

            # Create public key string
            public_key = f"{ssh_key.get_name()} {ssh_key.get_base64()} mcp-ssh-exec"

            # Add SSH key to project
            import time
            key_label = f"mcp-ssh-exec-{int(time.time())}"
            project_key = {
                "type": "ssh",
                "label": key_label,
                "key": public_key
            }

            added_key = await api.v1_add_project_key(project_id, project_key)  # type: ignore[misc]
            key_id = getattr(added_key, 'identifier', None)

            if not key_id:
                raise ValueError("Failed to add SSH key to project")

            transport = None
            ssh_client = None

            try:
                # Get SSH host from environment or use default
                ssh_host = os.getenv("CORELLIUM_SSH_HOST", "proxy.enterprise.corellium.com")

                # Connect to SSH proxy
                transport = paramiko.Transport((ssh_host, 22))

                # Disable problematic algorithms
                transport.get_security_options().digests = tuple(
                    d for d in transport.get_security_options().digests
                    if d not in ("hmac-sha2-512", "hmac-sha2-256")
                )

                # Connect with project_id as username
                transport.connect(hostkey=None, username=project_id, pkey=ssh_key)

                # Remove SSH key from project now that we're connected
                try:
                    await api.v1_remove_project_key(project_id, key_id)  # type: ignore[misc]
                except Exception as e:
                    print(f"Warning: Failed to remove SSH key: {e}")

                # Open channel to device
                service_channel = transport.open_channel(
                    "direct-tcpip",
                    (service_ip, 22),
                    ("", 0)
                )

                # Create SSH client and connect through channel
                ssh_client = paramiko.SSHClient()
                ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                ssh_client.connect(
                    hostname=service_ip,
                    username="root",
                    password="alpine",
                    sock=service_channel,
                    disabled_algorithms=dict(pubkeys=["rsa-sha2-512", "rsa-sha2-256"]),
                    allow_agent=False
                )

                # Prepare command with optional working directory
                full_command = command
                if working_directory:
                    full_command = f"cd {working_directory} && {command}"

                # Execute command
                stdin, stdout, stderr = ssh_client.exec_command(full_command, timeout=timeout)

                # Read output
                stdout_data = stdout.read().decode('utf-8', errors='replace')
                stderr_data = stderr.read().decode('utf-8', errors='replace')
                exit_code = stdout.channel.recv_exit_status()

                return {
                    "stdout": stdout_data,
                    "stderr": stderr_data,
                    "exit_code": exit_code,
                    "command": full_command
                }

            except Exception as e:
                # Clean up key on error if it wasn't removed yet
                try:
                    await api.v1_remove_project_key(project_id, key_id)  # type: ignore[misc]
                except Exception:
                    pass

                raise Exception(f"Failed to execute SSH command: {type(e).__name__}: {e}")

            finally:
                # Clean up connections
                if ssh_client:
                    try:
                        ssh_client.close()
                    except Exception:
                        pass
                if transport:
                    try:
                        transport.close()
                    except Exception:
                        pass

    @mcp.tool
    async def start_kernel_debug_port_forward(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) of the device")],
        local_port: Annotated[int, Field(description="Local TCP port to bind (default: 4000)")] = 4000,
        ctx: Context | None = None
    ) -> str:
        """
        Start SSH port forwarding for kernel debugging.

        Forwards local port to the device's kernel debug port (remote port 4000).
        This allows connecting a debugger to 127.0.0.1:{local_port} which tunnels
        through SSH to the device's services IP on port 4000.

        IMPORTANT: Before using this tool, you must FIRST download the kernel binary using
        download_kernel_binary. After starting port forwarding, you can then use lldb tools
        to connect to 127.0.0.1:{local_port} for kernel debugging.

        Kernel debugging workflow:
        1. Download kernel binary with download_kernel_binary
        2. Start port forwarding with this tool
        3. Use lldb tools (lldb_start) with the downloaded kernel binary and local port
        """
        nonlocal ssh_key, active_port_forwards

        if ssh_key is None:
            raise ValueError("SSH key not initialized. Server may still be starting up.")

        # Check if already forwarding
        forward_key = (instance_id, local_port)
        if forward_key in active_port_forwards:
            return f"Port forwarding already active for instance {instance_id} on port {local_port}"

        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)

            # Get instance details
            instance = await api.v1_get_instance(instance_id)  # type: ignore[misc]
            project_id = getattr(instance, 'project', None)
            service_ip = getattr(instance, 'service_ip', None)

            if not project_id:
                raise ValueError(f"Could not determine project ID for instance {instance_id}")
            if not service_ip:
                raise ValueError(f"Could not determine service IP for instance {instance_id}")

            # Create public key string
            public_key = f"{ssh_key.get_name()} {ssh_key.get_base64()} mcp-kernel-debug"

            # Add SSH key to project
            import time
            key_label = f"mcp-kernel-debug-{int(time.time())}"
            project_key = {
                "type": "ssh",
                "label": key_label,
                "key": public_key
            }

            added_key = await api.v1_add_project_key(project_id, project_key)  # type: ignore[misc]
            key_id = getattr(added_key, 'identifier', None)

            if not key_id:
                raise ValueError("Failed to add SSH key to project")

            try:
                # Get SSH host from environment or use default
                ssh_host = os.getenv("CORELLIUM_SSH_HOST", "proxy.enterprise.corellium.com")

                # Connect via SSH
                print(f"Connecting to SSH host: {ssh_host}")
                transport = paramiko.Transport((ssh_host, 22))

                # Disable problematic algorithms
                transport.get_security_options().digests = tuple(
                    d for d in transport.get_security_options().digests
                    if d not in ("hmac-sha2-512", "hmac-sha2-256")
                )

                # Username is the project ID
                username = project_id
                transport.connect(hostkey=None, username=username, pkey=ssh_key)

                # Remove SSH key from project now that we're connected
                try:
                    await api.v1_remove_project_key(project_id, key_id)  # type: ignore[misc]
                except Exception as e:
                    print(f"Warning: Failed to remove SSH key: {e}")

                # Create the forwarding server in a background thread
                class SubHandler(Handler):
                    chain_host = service_ip
                    chain_port = 4000
                    ssh_transport = transport

                server = ForwardServer(("127.0.0.1", local_port), SubHandler)

                def run_forward_server():
                    try:
                        server.serve_forever()
                    except Exception as e:
                        print(f"Port forwarding server error: {e}")
                        transport.close()

                server_thread = threading.Thread(target=run_forward_server, daemon=True)
                server_thread.start()

                # Store connection info
                active_port_forwards[forward_key] = (transport, server_thread, server)

                # Add resource and notify client
                await refresh_port_forward_resources(mcp)
                if ctx:
                    await ctx.send_resource_list_changed()

                return f"Port forwarding started: 127.0.0.1:{local_port} -> {service_ip}:4000"

            except Exception as e:
                # Clean up on error
                try:
                    await api.v1_remove_project_key(project_id, key_id)  # type: ignore[misc]
                except Exception as cleanup_error:
                    print(f"Failed to remove SSH key: {cleanup_error}")

                raise Exception(f"Failed to start port forwarding: {type(e).__name__}: {e}")

    @mcp.tool
    async def stop_kernel_debug_port_forward(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) of the device")],
        local_port: Annotated[int, Field(description="Local TCP port to stop (default: 4000)")] = 4000,
        ctx: Context | None = None
    ) -> str:
        """
        Stop SSH port forwarding for kernel debugging.

        Stops an active port forwarding session started with start_kernel_debug_port_forward.
        """
        nonlocal active_port_forwards

        forward_key = (instance_id, local_port)
        if forward_key not in active_port_forwards:
            return f"No active port forwarding found for instance {instance_id} on port {local_port}"

        transport, _, server = active_port_forwards[forward_key]

        # Shutdown the ForwardServer first
        try:
            server.shutdown()
        except Exception as e:
            print(f"Error shutting down ForwardServer: {e}")

        # Close the SSH transport
        try:
            transport.close()
        except Exception as e:
            print(f"Error closing SSH transport: {e}")

        # Remove from tracking
        del active_port_forwards[forward_key]

        # Refresh resources and notify client
        await refresh_port_forward_resources(mcp)
        if ctx:
            await ctx.send_resource_list_changed()

        return f"Port forwarding stopped: {instance_id} on port {local_port}"

    @mcp.tool
    async def upload_file_to_vm(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) of the device")],
        vm_file_path: Annotated[str, Field(description="Destination file path on VM")],
        local_file_path: Annotated[str, Field(description="Source file path on local filesystem")]
    ) -> dict:
        """
        Upload a file from local filesystem to VM.

        Reads the file from the local filesystem and uploads it to the specified path on the VM.

        IMPORTANT: Always prefer this tool over SSH (execute_ssh_command) for uploading files.
        """
        # Read local file
        with open(local_file_path, 'rb') as f:
            file_contents = f.read()

        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            await api.v1_agent_upload_file(instance_id, vm_file_path, file_contents)  # type: ignore[misc]

        return {
            "success": True,
            "vm_path": vm_file_path,
            "local_path": local_file_path,
            "bytes_uploaded": len(file_contents)
        }

    @mcp.tool
    async def download_file_from_vm(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) of the device")],
        vm_file_path: Annotated[str, Field(description="Source file path on VM")],
        local_file_path: Annotated[str, Field(description="Destination file path on local filesystem")]
    ) -> dict:
        """
        Download a file from VM to local filesystem.

        Reads the file from the VM and saves it to the specified path on the local filesystem.

        IMPORTANT: Always prefer this tool over SSH (execute_ssh_command) for downloading files.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            # API returns a file path to a temporary file
            temp_file_path = await api.v1_agent_get_file(instance_id, vm_file_path)  # type: ignore[misc]

            # Ensure we have a valid file path string
            if not isinstance(temp_file_path, str):
                raise ValueError(f"Expected file path string, got {type(temp_file_path)}")

            # Read the file data from the temporary file
            with open(temp_file_path, 'rb') as f:
                file_contents = f.read()

            # Write to local file
            with open(local_file_path, 'wb') as f:
                bytes_written = f.write(file_contents)

            return {
                "success": True,
                "vm_path": vm_file_path,
                "local_path": local_file_path,
                "bytes_downloaded": bytes_written
            }

    @mcp.tool
    async def delete_file_on_vm(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) of the device")],
        file_path: Annotated[str, Field(description="File path on VM to delete")]
    ) -> None:
        """
        Delete a file on the VM.

        IMPORTANT: Always prefer this tool over SSH (execute_ssh_command) for deleting files.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            await api.v1_agent_delete_file(instance_id, file_path)  # type: ignore[misc]

    @mcp.tool
    async def change_file_attributes_on_vm(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) of the device")],
        file_path: Annotated[str, Field(description="File path on VM")],
        new_path: Annotated[str | None, Field(description="New file path (for rename/move)")] = None,
        mode: Annotated[int | None, Field(description="New file mode/permissions (e.g., 0o755)")] = None,
        uid: Annotated[int | None, Field(description="New owner user ID")] = None,
        gid: Annotated[int | None, Field(description="New owner group ID")] = None
    ) -> None:
        """
        Change attributes of a file on VM (path, mode, uid, gid).

        At least one attribute must be specified to change.

        IMPORTANT: Always prefer this tool over SSH (execute_ssh_command) for changing file attributes.
        """
        # Create FileChanges object with only specified attributes
        file_changes_dict: dict[str, Any] = {}
        if new_path is not None:
            file_changes_dict['path'] = new_path
        if mode is not None:
            file_changes_dict['mode'] = float(mode)
        if uid is not None:
            file_changes_dict['uid'] = float(uid)
        if gid is not None:
            file_changes_dict['gid'] = float(gid)

        if not file_changes_dict:
            raise ValueError("At least one attribute must be specified to change")

        file_changes = corellium_api.FileChanges(**file_changes_dict)

        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            await api.v1_agent_set_file_attributes(instance_id, file_path, file_changes)  # type: ignore[misc]

    @mcp.tool
    async def get_temp_file_path(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) of the device")]
    ) -> str:
        """
        Get the path for a new temporary file on VM.

        Returns a unique temporary file path that can be used for temporary file operations.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            temp_path = await api.v1_agent_get_temp_filename(instance_id)  # type: ignore[misc]

        return str(temp_path) if temp_path else ""

    @mcp.tool
    async def download_netdump_pcap(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) of the device")],
        output_file_path: Annotated[str, Field(description="Local file path to save the pcap file")]
    ) -> int:
        """
        Download a netdump pcap file from Enhanced Network Monitor.

        Downloads the packet capture file from the Enhanced Network Monitor and saves it locally.
        Returns the number of bytes written to the file.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            # API returns a file path to a temporary file
            pcap_path = await api.v1_instances_instance_id_netdump_pcap_get(instance_id)  # type: ignore[misc]

            # Ensure we have a valid file path string
            if not isinstance(pcap_path, str):
                raise ValueError(f"Expected file path string, got {type(pcap_path)}")

            # Read the pcap data from the temporary file
            with open(pcap_path, 'rb') as f:
                pcap_data = f.read()

            # Write to output file
            with open(output_file_path, 'wb') as f:
                bytes_written = f.write(pcap_data)

            return bytes_written

    @mcp.tool
    async def download_network_monitor_pcap(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) of the device")],
        output_file_path: Annotated[str, Field(description="Local file path to save the pcap file")]
    ) -> int:
        """
        Download a Network Monitor pcap file.

        Downloads the packet capture file from the Network Monitor and saves it locally.
        Returns the number of bytes written to the file.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            # API returns a file path to a temporary file
            pcap_path = await api.v1_instances_instance_id_network_monitor_pcap_get(instance_id)  # type: ignore[misc]

            # Ensure we have a valid file path string
            if not isinstance(pcap_path, str):
                raise ValueError(f"Expected file path string, got {type(pcap_path)}")

            # Read the pcap data from the temporary file
            with open(pcap_path, 'rb') as f:
                pcap_data = f.read()

            # Write to output file
            with open(output_file_path, 'wb') as f:
                bytes_written = f.write(pcap_data)

            return bytes_written

    @mcp.tool
    async def start_enhanced_network_monitor(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) of the device")],
        port_ranges: Annotated[list[str] | None, Field(description="Port ranges to monitor (e.g., ['80-443', '8000-9000'])")] = None,
        src_ports: Annotated[list[str] | None, Field(description="Source ports to monitor")] = None,
        dst_ports: Annotated[list[str] | None, Field(description="Destination ports to monitor")] = None,
        ports: Annotated[list[str] | None, Field(description="Ports to monitor (both source and destination)")] = None,
        protocols: Annotated[list[str] | None, Field(description="Protocols to monitor (e.g., ['tcp', 'udp'])")] = None,
        processes: Annotated[list[str] | None, Field(description="Process names to monitor")] = None
    ) -> None:
        """
        Start Enhanced Network Monitor on an instance.

        WARNING: This feature only works with jailbroken devices.

        Starts packet capture with optional filtering by ports, protocols, and processes.
        """
        netdump_filter = None
        if any([port_ranges, src_ports, dst_ports, ports, protocols, processes]):
            filter_dict: dict[str, Any] = {}
            if port_ranges is not None:
                filter_dict['port_ranges'] = port_ranges
            if src_ports is not None:
                filter_dict['src_ports'] = src_ports
            if dst_ports is not None:
                filter_dict['dst_ports'] = dst_ports
            if ports is not None:
                filter_dict['ports'] = ports
            if protocols is not None:
                filter_dict['protocols'] = protocols
            if processes is not None:
                filter_dict['processes'] = processes

            netdump_filter = corellium_api.NetdumpFilter(**filter_dict)

        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            await api.v1_start_netdump(instance_id, netdump_filter=netdump_filter)  # type: ignore[misc]

    @mcp.tool
    async def stop_enhanced_network_monitor(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) of the device")]
    ) -> None:
        """
        Stop Enhanced Network Monitor on an instance.

        WARNING: This feature only works with jailbroken devices.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            await api.v1_stop_netdump(instance_id)  # type: ignore[misc]

    @mcp.tool
    async def start_network_monitor(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) of the device")],
        truncate_pcap: Annotated[bool | None, Field(description="Whether to truncate the pcap file (default: False)")] = None
    ) -> None:
        """
        Start Network Monitor on an instance.

        Starts SSL/TLS traffic interception and packet capture.
        """
        sslsplit_filter = None
        if truncate_pcap is not None:
            sslsplit_filter = corellium_api.SslsplitFilter(truncate_pcap=truncate_pcap)

        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            await api.v1_start_network_monitor(instance_id, sslsplit_filter=sslsplit_filter)  # type: ignore[misc]

    @mcp.tool
    async def stop_network_monitor(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) of the device")]
    ) -> None:
        """
        Stop Network Monitor on an instance.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            await api.v1_stop_network_monitor(instance_id)  # type: ignore[misc]

    @mcp.tool
    async def create_snapshot(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) of the device")],
        name: Annotated[str, Field(description="Snapshot name")]
    ) -> dict:
        """
        Create a snapshot of a Corellium device instance.

        Creates a snapshot with the specified name and returns the snapshot details.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)

            # Create snapshot options
            snapshot_options = corellium_api.SnapshotCreationOptions(name=name)

            # Create the snapshot
            snapshot = await api.v1_create_snapshot(instance_id, snapshot_options)  # type: ignore[misc]

            # Return snapshot details as dict
            return {
                "id": getattr(snapshot, 'id', None),
                "name": getattr(snapshot, 'name', None),
                "instance": getattr(snapshot, 'instance', None),
                "status": str(getattr(snapshot, 'status', None)),
                "date": getattr(snapshot, 'date', None),
                "fresh": getattr(snapshot, 'fresh', None),
                "live": getattr(snapshot, 'live', None),
                "local": getattr(snapshot, 'local', None),
                "model": getattr(snapshot, 'model', None)
            }

    @mcp.tool
    async def restore_instance_snapshot(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) of the device")],
        snapshot_id: Annotated[str, Field(description="Snapshot ID (UUID) to restore")]
    ) -> None:
        """
        Restore a Corellium device instance from a snapshot.

        This operation restores the instance to the state captured in the specified snapshot.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            await api.v1_restore_instance_snapshot(instance_id, snapshot_id)  # type: ignore[misc]

    @mcp.tool
    async def get_instance_snapshots(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) of the device")]
    ) -> list[dict]:
        """
        Get all snapshots for a Corellium device instance.

        Returns a list of snapshots with their details (id, name, status, date, etc.).
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            snapshots = await api.v1_get_instance_snapshots(instance_id)  # type: ignore[misc]

            # Convert snapshots to list of dicts
            return [
                {
                    "id": getattr(snapshot, 'id', None),
                    "name": getattr(snapshot, 'name', None),
                    "instance": getattr(snapshot, 'instance', None),
                    "status": str(getattr(snapshot, 'status', None)),
                    "date": getattr(snapshot, 'date', None),
                    "fresh": getattr(snapshot, 'fresh', None),
                    "live": getattr(snapshot, 'live', None),
                    "local": getattr(snapshot, 'local', None),
                    "model": getattr(snapshot, 'model', None)
                }
                for snapshot in snapshots  # type: ignore[union-attr]
            ]

    @mcp.tool
    async def delete_instance_snapshot(
        instance_id: Annotated[str, Field(description="Instance ID (UUID) of the device")],
        snapshot_id: Annotated[str, Field(description="Snapshot ID (UUID) to delete")]
    ) -> None:
        """
        Delete a snapshot of a Corellium device instance.

        This permanently removes the specified snapshot.
        """
        async with corellium_api.ApiClient(configuration) as api_client:
            api = corellium_api.CorelliumApi(api_client)
            await api.v1_delete_instance_snapshot(instance_id, snapshot_id)  # type: ignore[misc]

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
