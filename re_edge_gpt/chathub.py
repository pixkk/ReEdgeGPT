import asyncio
import json
import ssl
import sys
from time import time
from typing import Generator
from typing import List
from typing import Union
from urllib import parse

import aiohttp
import certifi
import httpx

from .constants import DELIMITER, SYDNEY_INIT_HEADER, SYDNEY_HEADER
from .constants import HEADERS
from .constants import HEADERS_INIT_CONVER
from .conversation import Conversation
from .conversation_style import CONVERSATION_STYLE_TYPE
from .proxy import get_proxy
from .request import ChatHubRequest
from .upload_image import upload_image, upload_image_url
from .utilities import append_identifier
from .utilities import guess_locale

ssl_context = ssl.create_default_context()
ssl_context.load_verify_locations(certifi.where())


async def _initial_handshake(wss) -> None:
    await wss.send_str(append_identifier({"protocol": "json", "version": 1}))
    await wss.receive_str()
    await wss.send_str(append_identifier({"type": 6}))


class ChatHub:
    def __init__(
            self,
            conversation: Conversation,
            proxy: str = None,
            cookies: Union[List[dict], None] = None,
            mode: str = "Bing"
    ) -> None:
        self.aio_session = None
        self.request: ChatHubRequest
        self.loop: bool
        self.task: asyncio.Task
        self.mode = mode
        self.request = ChatHubRequest(
            conversation_signature=conversation.struct["conversationSignature"],
            client_id=conversation.struct["clientId"],
            conversation_id=conversation.struct["conversationId"],
        )
        self.conversation_id = conversation.struct["conversationId"] or self.request.conversation_id
        self.cookies = cookies
        self.proxy: str = get_proxy(proxy)
        if self.mode == "Bing":
            header = HEADERS_INIT_CONVER
        else:
            header = SYDNEY_INIT_HEADER
        self.session = httpx.AsyncClient(
            proxies=self.proxy,
            timeout=900,
            headers=header,
        )
        if conversation.struct.get("encryptedConversationSignature"):
            self.encrypted_conversation_signature = conversation.struct["encryptedConversationSignature"]
        else:
            self.encrypted_conversation_signature = None
        self.conversation = conversation

    async def ask_stream(
            self,
            prompt: str,
            wss_link: str = None,
            conversation_style: CONVERSATION_STYLE_TYPE = None,
            raw: bool = False,
            webpage_context: Union[str, None] = None,
            search_result: bool = False,
            locale: str = guess_locale(),
            # Use for attachment
            attachment: dict = None,
    ) -> Generator[bool, Union[dict, str], None]:
        """ """
        if self.encrypted_conversation_signature is not None:
            wss_link = wss_link or "wss://sydney.bing.com/sydney/ChatHub"
            wss_link += f"?sec_access_token={parse.quote(self.encrypted_conversation_signature)}"
        cookies = {}
        if self.cookies is not None:
            for cookie in self.cookies:
                cookies[cookie["name"]] = cookie["value"]
        self.aio_session = aiohttp.ClientSession(cookies=cookies)
        if self.mode == "Bing":
            header = HEADERS
        else:
            header = SYDNEY_HEADER
        # Check if websocket is closed
        wss = await self.aio_session.ws_connect(
            wss_link or "wss://sydney.bing.com/sydney/ChatHub",
            ssl=ssl_context,
            headers=header,
            proxy=self.proxy,
        )
        await _initial_handshake(wss)
        # Image
        image_url = None
        if attachment is not None:
            if attachment.get("image_url") is not None:
                response = await upload_image_url(**attachment, conversation_id=self.conversation_id)
            else:
                response = await upload_image(**attachment)
            if response:
                image_url = f"https://www.bing.com/images/blob?bcid={response}"
        # Construct a ChatHub request
        self.request.update(
            prompt=prompt,
            conversation_style=conversation_style,
            webpage_context=webpage_context,
            search_result=search_result,
            locale=locale,
            image_url=image_url,
        )
        # Send request
        await wss.send_str(append_identifier(self.request.struct))
        resp_txt = ""
        result_text = ""
        resp_txt_no_link = ""
        retry_count = 5
        while not wss.closed:
            msg = await wss.receive_str()
            if not msg:
                retry_count -= 1
                if retry_count == 0:
                    raise Exception("No response from server")
                continue
            if isinstance(msg, str):
                objects = msg.split(DELIMITER)
            else:
                continue
            for obj in objects:
                if int(time()) % 6 == 0:
                    await wss.send_str(append_identifier({"type": 6}))
                if obj is None or not obj:
                    continue
                response = json.loads(obj)
                if response.get("type") == 1 and response["arguments"][0].get("messages"):
                    if (response["arguments"][0]["messages"][0]["contentOrigin"] != "Apology") and not raw:
                        try:
                            resp_txt = result_text + response["arguments"][0][
                                "messages"
                            ][0]["adaptiveCards"][0]["body"][0].get("text", "")
                            resp_txt_no_link = result_text + response["arguments"][0][
                                "messages"
                            ][0].get("text", "")
                            if response["arguments"][0]["messages"][0].get(
                                    "messageType",
                            ):
                                resp_txt = (
                                        resp_txt
                                        + response["arguments"][0]["messages"][0][
                                            "adaptiveCards"
                                        ][0]["body"][0]["inlines"][0].get("text")
                                        + "\n"
                                )
                                result_text = (
                                        result_text
                                        + response["arguments"][0]["messages"][0][
                                            "adaptiveCards"
                                        ][0]["body"][0]["inlines"][0].get("text")
                                        + "\n"
                                )
                        except KeyError:
                            pass
                    if not raw:
                        yield False, resp_txt
                elif response.get("type") == 2:
                    if response["item"]["result"].get("error"):
                        await self.close()
                        raise Exception(
                            f"{response['item']['result']['value']}: {response['item']['result']['message']}",
                        )
                    if response["item"]["messages"][-1]["contentOrigin"] == "Apology" and resp_txt:
                        response["item"]["messages"][-1]["text"] = resp_txt_no_link
                        response["item"]["messages"][-1]["adaptiveCards"][0]["body"][0][
                            "text"
                        ] = resp_txt
                        print(
                            "Preserved the message from being deleted",
                            file=sys.stderr,
                        )
                    await wss.close()
                    if not self.aio_session.closed:
                        await self.aio_session.close()
                    yield True, response
                    return
                if response.get("type") != 2:
                    if response.get("type") == 6:
                        await wss.send_str(append_identifier({"type": 6}))
                    elif response.get("type") == 7:
                        await wss.send_str(append_identifier({"type": 7}))
                    elif raw:
                        yield False, response

    async def close(self) -> None:
        await self.session.aclose()

    async def get_conversation(self) -> dict:
        return {
            "conversation_id": self.conversation_id,
            "client_id": self.request.client_id,
            "encrypted_conversation_signature": self.encrypted_conversation_signature,
            "conversation_signature": self.request.conversation_signature,
        }

    async def set_conversation(self, conversation_dict: dict) -> None:
        self.conversation.struct["conversationId"] = conversation_dict.get("conversation_id")
        self.conversation.struct["client_id"] = conversation_dict.get("client_id")
        self.conversation.struct[
            "encrypted_conversation_signature"] = conversation_dict.get("encrypted_conversation_signature")
        self.conversation.struct["conversation_signature"] = conversation_dict.get("conversation_signature")
