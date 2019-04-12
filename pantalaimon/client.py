import asyncio
from pprint import pformat
from typing import Any, Dict, Optional

from nio import (AsyncClient, ClientConfig, EncryptionError,
                 GroupEncryptionError, KeysQueryResponse, MegolmEvent,
                 RoomEncryptedEvent, SyncResponse)
from nio.store import SqliteStore

from pantalaimon.log import logger


class PanClient(AsyncClient):
    """A wrapper class around a nio AsyncClient extending its functionality."""

    def __init__(
            self,
            homeserver,
            user="",
            device_id="",
            store_path="",
            config=None,
            ssl=None,
            proxy=None
    ):
        config = config or ClientConfig(store=SqliteStore, store_name="pan.db")
        super().__init__(homeserver, user, device_id, store_path, config,
                         ssl, proxy)

        self.task = None
        self.loop_stopped = asyncio.Event()
        self.synced = asyncio.Event()

    def verify_devices(self, changed_devices):
        # Verify new devices automatically for now.
        for user_id, device_dict in changed_devices.items():
            for device in device_dict.values():
                if device.deleted:
                    continue

                logger.info("Automatically verifying device {} of "
                            "user {}".format(device.id, user_id))
                self.verify_device(device)

    def start_loop(self):
        """Start a loop that runs forever and keeps on syncing with the server.

        The loop can be stopped with the stop_loop() method.
        """
        loop = asyncio.get_event_loop()
        task = loop.create_task(self.loop())
        self.task = task
        return task

    async def loop(self):
        self.loop_running = True
        self.loop_stopped.clear()

        logger.info(f"Starting sync loop for {self.user_id}")

        try:
            while True:
                if not self.logged_in:
                    # TODO login
                    pass

                # TODO use user lazy loading here
                response = await self.sync(30000)

                if self.should_upload_keys:
                    await self.keys_upload()

                if self.should_query_keys:
                    key_query_response = await self.keys_query()
                    if isinstance(key_query_response, KeysQueryResponse):
                        self.verify_devices(key_query_response.changed)

                if not isinstance(response, SyncResponse):
                    # TODO error handling
                    pass

                self.synced.set()
                self.synced.clear()

        except asyncio.CancelledError:
            logger.info("Stopping the sync loop")
            self.loop_running = False
            self.loop_stopped.set()

    async def loop_stop(self):
        """Stop the client loop."""
        if not self.task:
            return

        self.task.cancel()
        await self.loop_stopped.wait()

    async def encrypt(self, room_id, msgtype, content):
        try:
            return super().encrypt(
                room_id,
                msgtype,
                content
            )
        except GroupEncryptionError:
            await self.share_group_session(room_id)
            return super().encrypt(
                room_id,
                msgtype,
                content
            )

    def pan_decrypt_event(self, event_dict, room_id=None):
        # type: (Dict[Any, Any], Optional[str]) -> ()
        event = RoomEncryptedEvent.parse_event(event_dict)

        if not event.room_id:
            event.room_id = room_id

        if not isinstance(event, MegolmEvent):
            logger.warn("Encrypted event is not a megolm event:"
                        "\n{}".format(pformat(event_dict)))
            return None

        try:
            decrypted_event = self.decrypt_event(event)
            logger.info("Decrypted event: {}".format(decrypted_event))
            event_dict["type"] = "m.room.message"

            # TODO support other event types
            # This should be best done in nio, modify events so they
            # keep the dictionary from which they are built in a source
            # attribute.
            event_dict["content"] = {
                "msgtype": "m.text",
                "body": decrypted_event.body
            }

            if decrypted_event.formatted_body:
                event_dict["content"]["formatted_body"] = (
                    decrypted_event.formatted_body)
                event_dict["content"]["format"] = decrypted_event.format

            event_dict["decrypted"] = True
            event_dict["verified"] = decrypted_event.verified

        except EncryptionError as error:
            logger.warn(error)
            return

    def decrypt_messages_body(self, body):
        # type: (Dict[Any, Any]) -> Dict[Any, Any]
        """Go through a messages response and decrypt megolm encrypted events.

        Args:
            body (Dict[Any, Any]): The dictionary of a Sync response.

        Returns the json response with decrypted events.
        """
        if "chunk" not in body:
            return body

        logger.info("Decrypting room messages")

        for event in body["chunk"]:
            if "type" not in event:
                continue

            if event["type"] != "m.room.encrypted":
                logger.debug("Event is not encrypted: "
                             "\n{}".format(pformat(event)))
                continue

            self.pan_decrypt_event(event)

        return body

    def decrypt_sync_body(self, body):
        # type: (Dict[Any, Any]) -> Dict[Any, Any]
        """Go through a json sync response and decrypt megolm encrypted events.

        Args:
            body (Dict[Any, Any]): The dictionary of a Sync response.

        Returns the json response with decrypted events.
        """
        logger.info("Decrypting sync")
        for room_id, room_dict in body["rooms"]["join"].items():
            try:
                if not self.rooms[room_id].encrypted:
                    logger.info("Room {} is not encrypted skipping...".format(
                        self.rooms[room_id].display_name
                    ))
                    continue
            except KeyError:
                logger.info("Unknown room {} skipping...".format(room_id))
                continue

            for event in room_dict["timeline"]["events"]:
                self.pan_decrypt_event(event, room_id)

        return body