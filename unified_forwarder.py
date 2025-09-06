"""
统一消息转发器 - 支持用户间连线和客服连接的消息转发
"""

from asyncio.log import logger
from astrbot.api.star import Context
from astrbot.core.platform import AstrMessageEvent
from astrbot.api.event import MessageChain
from astrbot.core.message.components import (
    Plain, Image, Video, Record, File, Face, WechatEmoji, 
    At, Music, BaseMessageComponent
)
from .connection_manager import Connection, ConnectionType


class UnifiedMessageForwarder:
    """统一消息转发器"""
    
    def __init__(self, context: Context):
        self.context = context
    
    async def forward_message(self, connection: Connection, sender_event: AstrMessageEvent) -> bool:
        """转发消息到连接的另一方"""
        if not connection.is_connected():
            return False
        
        try:
            # 获取对方信息
            sender_origin = sender_event.unified_msg_origin
            other_party = connection.get_other_party(sender_origin)
            
            if not other_party:
                return False
            
            other_user_id, other_unified_msg_origin = other_party
            
            # 提取完整的消息链（包含多媒体）
            original_message_chain = self._extract_message_chain(sender_event)
            if not original_message_chain.chain:
                return False
            
            # 获取消息摘要用于日志
            message_summary = self._get_message_summary(original_message_chain)

            # 构建转发的消息链
            forwarded_components = []
            
            # 添加原始消息的所有组件
            for component in original_message_chain.chain:
                forwarded_components.append(component)
            
            forwarded_message_chain = MessageChain(forwarded_components)
            
            # 发送消息
            success = await self.send_message_chain(other_unified_msg_origin, forwarded_message_chain)
            
            if success:
                # 记录到连接历史
                sender_role = "用户" if sender_origin == connection.initiator_unified_msg_origin else "接收方"
                connection.add_message_to_history(sender_role, message_summary)
                logger.debug(f"Forwarded multimedia message: {message_summary}")
            
            return success
            
        except Exception as e:
            logger.error(f"Error forwarding message: {e}")
            return False
    
    async def send_message_chain(self, unified_msg_origin: str, message_chain: MessageChain) -> bool:
        """向指定用户发送多媒体消息链"""
        try:
            # 验证和清理消息链
            sanitized_chain = self._sanitize_message_chain(message_chain)
            
            # 使用Context的send_message方法发送多媒体消息
            success = await self.context.send_message(unified_msg_origin, sanitized_chain)
            
            if success:
                message_summary = self._get_message_summary(sanitized_chain)
                logger.debug(f"Message chain sent to {unified_msg_origin}: {message_summary}")
            else:
                logger.debug(f"Failed to find platform for: {unified_msg_origin}")
            
            return success
        except Exception as e:
            logger.error(f"Error sending message chain: {e}")
            return False
    
    async def send_message(self, unified_msg_origin: str, content: str) -> bool:
        """向指定用户发送纯文本消息（保持向后兼容）"""
        try:
            # 创建纯文本消息链
            message_chain = MessageChain([Plain(content)])
            
            # 调用新的send_message_chain方法
            return await self.send_message_chain(unified_msg_origin, message_chain)
        except Exception as e:
            logger.error(f"Error sending message: {e}")
            return False
    
    async def notify_connection_request(self, connection: Connection, receiver_id: str) -> bool:
        """通知用户有连接请求"""
        try:
            if connection.connection_type == ConnectionType.USER_TO_USER:
                # 用户间连线请求
                description_text = f"\n说明: {connection.initiator_description}" if connection.initiator_description else ""
                notification = (
                    f"🔗 连线请求\n"
                    f"来自用户: {connection.initiator_name}\n"
                    f"连接ID: {connection.connection_id}\n"
                    f"请求时间: {connection.created_at.strftime('%Y-%m-%d %H:%M:%S')}"
                    f"{description_text}\n\n"
                    f"回复 同意连线 {connection.connection_id} 接受连线请求\n"
                    f"回复 拒绝连线 {connection.connection_id} 拒绝连线请求"
                )
            else:  # CUSTOMER_SERVICE
                # 客服请求
                description_text = f"\n问题描述: {connection.initiator_description}" if connection.initiator_description else ""
                notification = (
                    f"🔔 客服请求通知\n"
                    f"用户ID: {connection.initiator_name}\n"
                    f"会话ID: {connection.connection_id}\n"
                    f"请求时间: {connection.created_at.strftime('%Y-%m-%d %H:%M:%S')}"
                    f"{description_text}\n\n"
                    f"回复 客服接入 {connection.connection_id} 接入此会话"
                )
            # todo 平台设配
            if receiver_id.startswith("wxid_"):
                receiver_id = f"wechat-fluidocat:FriendMessage:{receiver_id}"
            await self.send_message(receiver_id, notification)
            return True
        except Exception as e:
            logger.error(f"Error sending connection notification: {e}")
            return False
    
    async def notify_connection_established(self, connection: Connection) -> bool:
        """通知双方连接已建立"""
        try:
            if connection.connection_type == ConnectionType.USER_TO_USER:
                # 用户间连线
                initiator_msg = f"✅ 连线已建立\n与用户 {connection.receiver_name} 的连线已成功建立\n现在可以直接发送消息进行交流"
                receiver_msg = f"✅ 连线已建立\n与用户 {connection.initiator_name} 的连线已成功建立\n现在可以直接发送消息进行交流"
            else:  # CUSTOMER_SERVICE
                # 客服连接
                initiator_msg = f"✅ 客服已接入\n客服 {connection.receiver_name} 已接入会话\n现在可以直接发送消息咨询问题"
                receiver_msg = f"✅ 成功接入客服会话\n用户 {connection.initiator_name} 的会话已接入\n现在可以直接发送消息回复用户"
            
            # 发送通知给发起方
            await self.send_message(connection.initiator_unified_msg_origin, initiator_msg)
            
            # 发送通知给接收方
            if connection.receiver_unified_msg_origin:
                await self.send_message(connection.receiver_unified_msg_origin, receiver_msg)
            
            return True
            
        except Exception as e:
            logger.error(f"Error notifying connection established: {e}")
            return False
    
    async def notify_connection_ended(self, connection: Connection, ended_by: str) -> bool:
        """通知双方连接已结束"""
        try:
            if connection.connection_type == ConnectionType.USER_TO_USER:
                message = f"👋 连线已结束\n与用户的连线已由 {ended_by} 结束"
            else:  # CUSTOMER_SERVICE
                message = f"👋 客服会话已结束\n客服会话已结束，感谢您的使用"
            
            # 通知发起方
            await self.send_message(connection.initiator_unified_msg_origin, message)
            
            # 通知接收方
            if connection.receiver_unified_msg_origin:
                await self.send_message(connection.receiver_unified_msg_origin, message)
            
            return True
            
        except Exception as e:
            logger.error(f"Error notifying connection ended: {e}")
            return False
    
    def _extract_message_content(self, event: AstrMessageEvent) -> str:
        """提取消息内容（仅用于命令检测）"""
        try:
            # 获取消息组件
            messages = event.get_messages()
            content_parts = []
            
            for msg in messages:
                # 仅提取文本内容用于命令检测
                if isinstance(msg, Plain) and hasattr(msg, 'text'):
                    content_parts.append(msg.text)
                elif hasattr(msg, 'text'):
                    content_parts.append(msg.text)
            
            return ''.join(content_parts).strip() if content_parts else ""
            
        except Exception as e:
            logger.error(f"Error extracting message content: {e}")
            return ""
    
    def _extract_message_chain(self, event: AstrMessageEvent) -> MessageChain:
        """提取完整的消息链（包含多媒体消息）"""
        try:
            messages = event.get_messages()
            components = []
            support_comp = (Plain, Image, Video, WechatEmoji, At, Music, File)
            for msg in messages:
                if isinstance(msg, support_comp) and self._validate_message_component(msg):
                    components.append(msg)

            return MessageChain(components)
            
        except Exception as e:
            logger.error(f"Error extracting message chain: {e}")
            return MessageChain()
    
    def _validate_message_component(self, component: BaseMessageComponent) -> bool:
        """验证消息组件是否有效"""
        try:
            if isinstance(component, Plain):
                return bool(component.text and component.text.strip())
            elif isinstance(component, Image):
                return bool(component.file or component.url)
            elif isinstance(component, Video):
                return bool(component.file)
            elif isinstance(component, Record):
                return bool(component.file)
            elif isinstance(component, File):
                return bool(component.file_ or component.url)
            elif isinstance(component, Face):
                return component.id is not None
            elif isinstance(component, WechatEmoji):
                return bool(component.md5 or component.cdnurl)
            elif isinstance(component, At):
                return bool(component.qq) or bool(component.name)
            elif isinstance(component, Music):
                return bool(component.audio or component.url)
            else:
                # 其他类型默认为有效
                return False
        except Exception as e:
            logger.error(f"Error validating component {type(component)}: {e}")
            return False
    
    def _sanitize_message_chain(self, message_chain: MessageChain) -> MessageChain:
        """清理和验证消息链，移除无效组件"""
        try:
            valid_components = []
            
            for component in message_chain.chain:
                if self._validate_message_component(component):
                    valid_components.append(component)
                else:
                    logger.warning(f"Removed invalid component: {type(component)}")
            
            # 如果没有有效组件，添加一个默认文本
            if not valid_components:
                valid_components = [Plain("无效消息内容")]
            
            return MessageChain(valid_components)
            
        except Exception as e:
            logger.error(f"Error sanitizing message chain: {e}")
            return MessageChain([Plain("消息处理失败")])
    
    def _get_message_summary(self, message_chain: MessageChain) -> str:
        """获取消息摘要（用于日志和通知）"""
        try:
            summary_parts = []
            
            for component in message_chain.chain:
                if isinstance(component, Plain):
                    if component.text and component.text.strip():
                        summary_parts.append(component.text.strip())
                elif isinstance(component, Image):
                    summary_parts.append("[图片]")
                elif isinstance(component, Video):
                    summary_parts.append("[视频]")
                elif isinstance(component, Record):
                    summary_parts.append("[语音]")
                elif isinstance(component, File):
                    file_name = getattr(component, 'name', '文件')
                    summary_parts.append(f"[文件: {file_name}]")
                elif isinstance(component, Face):
                    summary_parts.append("[表情]")
                elif isinstance(component, WechatEmoji):
                    summary_parts.append("[表情包]")
                elif isinstance(component, At):
                    at_name = getattr(component, 'name', getattr(component, 'qq', 'someone'))
                    summary_parts.append(f"[@{at_name}]")
                elif isinstance(component, Music):
                    music_title = getattr(component, 'title', '音乐')
                    summary_parts.append(f"[音乐: {music_title}]")
                else:
                    # 其他类型的组件
                    summary_parts.append(f"[{component.type}]")
            
            return ' '.join(summary_parts) if summary_parts else "无消息内容"
            
        except Exception as e:
            logger.error(f"Error getting message summary: {e}")
            return "消息内容获取失败"
        """获取消息摘要（用于日志和通知）"""
        try:
            summary_parts = []
            
            for component in message_chain.chain:
                if isinstance(component, Plain):
                    if component.text and component.text.strip():
                        summary_parts.append(component.text.strip())
                elif isinstance(component, Image):
                    summary_parts.append("[图片]")
                elif isinstance(component, Video):
                    summary_parts.append("[视频]")
                elif isinstance(component, Record):
                    summary_parts.append("[语音]")
                elif isinstance(component, File):
                    file_name = getattr(component, 'name', '文件')
                    summary_parts.append(f"[文件: {file_name}]")
                elif isinstance(component, Face):
                    summary_parts.append("[表情]")
                elif isinstance(component, WechatEmoji):
                    summary_parts.append("[表情包]")
                elif isinstance(component, At):
                    at_name = getattr(component, 'name', getattr(component, 'qq', 'someone'))
                    summary_parts.append(f"[@{at_name}]")
                elif isinstance(component, Music):
                    music_title = getattr(component, 'title', '音乐')
                    summary_parts.append(f"[音乐: {music_title}]")
                else:
                    # 其他类型的组件
                    summary_parts.append(f"[{component.type}]")
            
            return ' '.join(summary_parts) if summary_parts else "无消息内容"
            
        except Exception as e:
            logger.error(f"Error getting message summary: {e}")
            return "消息内容获取失败"