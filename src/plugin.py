# -*- coding: utf-8 -*-
"""Feishu Plus 渠道插件入口。

注册一个继承自内置 ``FeishuChannel`` 的 ``FeishuPlusChannel``，
channel key = ``feishu_plus``。控制台会把它作为独立渠道卡片显示，
与内置 feishu 并列；二者共用同一飞书应用时只能启用其一。
"""
from __future__ import annotations

import logging

from qwenpaw.plugins.api import PluginApi

logger = logging.getLogger(__name__)


class FeishuPlusPlugin:
    """Feishu Plus 渠道插件。"""

    def register(self, api: PluginApi) -> None:
        from .channel import FeishuPlusChannel

        api.register_channel(
            channel_class=FeishuPlusChannel,
            label={
                "zh": "飞书+",
                "en": "Feishu Plus",
            },
            description="继承内置飞书渠道，修复话题内审批卡片发送路径，尝试话题内流式输出，并以 / 开头的命令消息跳过引用内容获取。需停用内置「飞书」渠道。",
            icon="https://gw.alicdn.com/imgextra/i4/O1CN01jsn08m225euyUoaFN_!!6000000007069-2-tps-400-400.png",
            config_fields=[
                {
                    "name": "app_id",
                    "label": {"zh": "App ID", "en": "App ID"},
                    "type": "password",
                    "required": True,
                    "placeholder": "cli_xxxx",
                },
                {
                    "name": "app_secret",
                    "label": {"zh": "App Secret", "en": "App Secret"},
                    "type": "password",
                    "required": True,
                },
                {
                    "name": "encrypt_key",
                    "label": {
                        "zh": "Encrypt Key",
                        "en": "Encrypt Key",
                    },
                    "placeholder": "Optional, for event encryption",
                    "type": "password",
                    "required": False,
                },
                {
                    "name": "verification_token",
                    "label": {
                        "zh": "Verification Token",
                        "en": "Verification Token",
                    },
                    "placeholder": "Optional",
                    "type": "password",
                    "required": False,
                },
                {
                    "name": "domain",
                    "label": {"zh": "地区", "en": "Region"},
                    "type": "text",
                    "required": False,
                    "default": "feishu",
                    "help": {
                        "zh": "国内用户使用feishu，海外用户使用 lark",
                        "en": "Choose `feishu` for China or `lark` for International",
                    }
                },
                {
                    "name": "media_dir",
                    "label": {"zh": "媒体文件目录", "en": "Media Directory"},
                    "type": "text",
                    "required": False
                },
                {
                    "name": "streaming_enabled",
                    "label": {
                        "zh": "流式输出",
                        "en": "Streaming Output",
                    },
                    "type": "switch",
                    "required": False,
                    "default": False,
                    "help": {
                        "zh": "需要在飞书开放平台权限管理界面开通 cardkit:card:write 权限",
                        "en": "Requires enabling cardkit:card:write permission in the Feishu Open Platform permission management page",
                    },
                },
                {
                    "name": "share_session_in_group",
                    "label": {
                        "zh": "群聊共享上下文",
                        "en": "Share Session in Group",
                    },
                    "type": "switch",
                    "required": False,
                    "default": False,
                    "help": {
                        "zh": "启用时，群内所有成员共享同一会话上下文；禁用时，每位成员维护各自独立的会话。",
                        "en": "When enabled, all group members share the same conversation context. When disabled, each member has their own independent context.",
                    },
                },
                {
                    "name": "access_control_dm",
                    "label": {
                        "zh": "私聊访问控制",
                        "en": "DM Access Control",
                    },
                    "type": "switch",
                    "required": False,
                    "default": False,
                    "help": {
                        "zh": "开启后，只有白名单用户可以通过私聊与机器人互动",
                        "en": "When enabled, only whitelisted users can interact with the bot in direct messages",
                    },
                },
                {
                    "name": "access_control_group",
                    "label": {
                        "zh": "群聊访问控制",
                        "en": "Group Access Control",
                    },
                    "type": "switch",
                    "required": False,
                    "default": False,
                    "help": {
                        "zh": "开启后，只有白名单用户可以在群聊中与机器人互动",
                        "en": "When enabled, only whitelisted users can interact with the bot in group chats",
                    },
                },
                {
                    "name": "require_mention",
                    "label": {
                        "zh": "需要 @提及",
                        "en": "Require @Mention",
                    },
                    "type": "switch",
                    "required": False,
                    "default": False,
                    "help": {
                        "zh": "开启后，群聊中仅在被 @提及 时才会回复",
                        "en": "enabled, bot only responds in group chats when explicitly @mentioned",
                    },
                },
            ],
        )
        logger.info("✓ Feishu Plus channel registered")


plugin = FeishuPlusPlugin()
