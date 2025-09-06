"""
统一连接服务插件 - 支持用户间连线和客服连接
"""

import asyncio
from asyncio.log import logger
from typing import Dict
from astrbot.api.star import Star, Context
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.utils.session_waiter import (
    SessionFilter,
    session_waiter,
    SessionController
)
from .connection_manager import ConnectionManager, ConnectionType, ConnectionStatus
from .unified_forwarder import UnifiedMessageForwarder


class MessageForwardPlugin(Star):
    """统一连接插件主类 - 支持用户间连线和客服连接"""
    
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.connection_manager = ConnectionManager()
        self.message_forwarder = UnifiedMessageForwarder(context)
        self.admin_ids = self.parse_admin_ids()
        
        # 存储当前活跃的消息拦截状态
        self.message_intercept_sessions: Dict[str, str] = {}  # user_unified_msg_origin -> connection_id
        
        # 启动定期清理任务
        asyncio.create_task(self._periodic_cleanup())

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        self.parse_admin_ids()

    def parse_admin_ids(self):
        admin_unified_msg_origin = set()
        csr = self.config.get("csr", {})
        for platform_config in csr.values():
            user_ids = platform_config.get("user_id",[])
            platform_id = platform_config.get("platform_id", "")
            if user_ids and platform_id:
                umo = [f"{platform_id}:FriendMessage:{uid}" for uid in user_ids if uid]
                admin_unified_msg_origin.update(umo)
        for platform_id in self.context.platform_manager._inst_map.keys():
            _config = self.context.get_config(f"{platform_id}:FriendMessage:*")
            if user_ids:=_config["admins_id"]:
                umo = [f"{platform_id}:FriendMessage:{uid}" for uid in user_ids if uid]
                admin_unified_msg_origin.update(umo)

        self.admin_ids = admin_unified_msg_origin
        return admin_unified_msg_origin

    async def _periodic_cleanup(self):
        """定期清理过期连接"""
        while True:
            try:
                await asyncio.sleep(300)  # 每5分钟清理一次
                await self.connection_manager.cleanup_expired_connections()
            except Exception as e:
                logger.error(f"Error in periodic cleanup: {e}")
    
    @filter.command("连线")
    async def user_connect(self, event: AstrMessageEvent, target_user_id: str = None, description: str = None):
        """用户发起连线请求"""
        if not target_user_id:
            yield event.plain_result("请提供目标用户ID，格式: 连线 <用户ID> [说明]")
            return
        
        # 检查是否已有活跃连接
        existing_connection = self.connection_manager.get_connection_by_user(event.unified_msg_origin)
        if existing_connection:
            if existing_connection.status == ConnectionStatus.REQUESTING:
                yield event.plain_result(
                    f"您已有连线请求在处理中，连接ID: {existing_connection.connection_id}\n"
                    f"请等待对方回复或使用 取消连线 取消请求"
                )
                return
            elif existing_connection.status == ConnectionStatus.CONNECTED:
                yield event.plain_result(
                    f"您当前已有活跃连接，连接ID: {existing_connection.connection_id}\n"
                    f"请先结束当前连接再发起新连线"
                )
                return
        
        # 创建连线请求
        connection_id = await self.connection_manager.create_connection(
            ConnectionType.USER_TO_USER, event, target_user_id, description
        )
        connection = self.connection_manager.get_connection(connection_id)
        
        if not connection:
            yield event.plain_result("创建连线请求失败，请稍后重试")
            return
        
        # 发送通知给目标用户
        await self.message_forwarder.notify_connection_request(connection, target_user_id)
        
        # 回复发起者
        yield event.plain_result(
            f"✅ 已发送连线请求\n"
            f"连接ID: {connection_id}\n"
            f"目标用户: {target_user_id}\n"
            f"请等待对方回复..."
        )
        
        # 启动连线等待
        await self._start_connection_wait(event, connection_id)
    
    @filter.command("同意连线")
    async def accept_connection(self, event: AstrMessageEvent, connection_id: str = None):
        """用户同意连线请求"""
        if not connection_id:
            yield event.plain_result("请提供连接ID，格式: 同意连线 <连接ID>")
            return
        
        # 检查用户是否已有活跃连接
        existing_connection = self.connection_manager.get_connection_by_user(event.unified_msg_origin)
        if existing_connection:
            yield event.plain_result("您当前已有活跃连接，请先结束当前连接")
            return
        
        # 尝试接受连接
        success = await self.connection_manager.accept_connection(connection_id, event)
        if not success:
            yield event.plain_result(
                f"接受失败：连接 {connection_id} 不存在、已过期或已被其他用户接受"
            )
            return
        
        connection = self.connection_manager.get_connection(connection_id)
        if not connection:
            yield event.plain_result("连接接受失败")
            return
        
        # 设置消息拦截
        initiator_origin = connection.initiator_unified_msg_origin
        receiver_origin = connection.receiver_unified_msg_origin
        
        self.message_intercept_sessions[initiator_origin] = connection_id
        self.message_intercept_sessions[receiver_origin] = connection_id
        
        # 通知双方连接已建立
        await self.message_forwarder.notify_connection_established(connection)
        
        # 启动连接等待
        await self._start_connection_wait(event, connection_id)
    
    @filter.command("拒绝连线")
    async def reject_connection(self, event: AstrMessageEvent, connection_id: str = None):
        """用户拒绝连线请求"""
        if not connection_id:
            yield event.plain_result("请提供连接ID，格式: 拒绝连线 <连接ID>")
            return
        
        connection = self.connection_manager.get_connection(connection_id)
        if not connection or connection.status != ConnectionStatus.REQUESTING:
            yield event.plain_result(f"连接 {connection_id} 不存在或已无效")
            return
        
        # 通知发起者连接被拒绝
        rejection_msg = f"❌ 连线请求被拒绝\n用户 {event.get_sender_id()} 拒绝了您的连线请求"
        await self.message_forwarder.send_message(connection.initiator_unified_msg_origin, rejection_msg)
        
        # 结束连接
        await self.connection_manager.end_connection(connection_id)
        
        yield event.plain_result(f"✅ 已拒绝连线请求 {connection_id}")
    
    @filter.command("转人工")
    async def request_human_service(self, event: AstrMessageEvent, description: str = None):
        """用户请求人工客服"""
        user_id = str(event.get_sender_id())
        
        # 检查是否已有活跃连接
        existing_connection = self.connection_manager.get_connection_by_user(event.unified_msg_origin)
        if existing_connection:
            if existing_connection.connection_type == ConnectionType.CUSTOMER_SERVICE:
                if existing_connection.status == ConnectionStatus.REQUESTING:
                    yield event.plain_result(
                        f"您已在等待队列中，会话ID: {existing_connection.connection_id}\n"
                        f"请等待客服接入，预计等待时间: 5-10分钟"
                    )
                    return
                elif existing_connection.status == ConnectionStatus.CONNECTED:
                    yield event.plain_result(
                        f"您当前已有客服在线，会话ID: {existing_connection.connection_id}\n"
                        f"请直接发送消息与客服交流"
                    )
                    return
            else:
                yield event.plain_result("您当前有其他活跃连接，请先结束后再请求客服")
                return
        
        # 创建客服连接
        connection_id = await self.connection_manager.create_connection(
            ConnectionType.CUSTOMER_SERVICE, event, None, description
        )
        connection = self.connection_manager.get_connection(connection_id)
        
        if not connection:
            yield event.plain_result("创建客服会话失败，请稍后重试")
            return
        
        # 通知管理员
        if not self.admin_ids:
            yield event.plain_result("暂无客服在线，请稍后重试")
            await self.connection_manager.end_connection(connection_id)
            return
        
        # 发送通知给所有管理员
        for admin_id in self.admin_ids:
            await self.message_forwarder.notify_connection_request(connection, admin_id)
        
        # 进入会话等待模式
        yield event.plain_result(
            f"✅ 已为您创建客服会话\n"
            f"会话ID: {connection_id}\n"
            f"正在通知客服，请稍候...\n"
            f"使用 结束连线 可以退出会话"
        )
        
        # 启动连接等待
        await self._start_connection_wait(event, connection_id)
    
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("客服接入")
    async def admin_connect(self, event: AstrMessageEvent, connection_id: str = None):
        """管理员接入客服会话"""
        if not connection_id:
            yield event.plain_result("请提供会话ID，格式: 客服接入 <会话ID>")
            return
        
        admin_id = str(event.get_sender_id())
        
        # 检查管理员是否已有活跃连接
        existing_connection = self.connection_manager.get_connection_by_user(event.unified_msg_origin)
        if existing_connection:
            yield event.plain_result(
                f"您已接入会话 {existing_connection.connection_id}，请先结束当前会话"
            )
            return
        
        # 尝试连接到指定会话
        success = await self.connection_manager.accept_connection(connection_id, event)
        if not success:
            yield event.plain_result(
                f"接入失败：会话 {connection_id} 不存在、已过期或已有其他客服接入"
            )
            return
        
        connection = self.connection_manager.get_connection(connection_id)
        if not connection:
            yield event.plain_result("会话接入失败")
            return
        
        # 设置消息拦截
        self.message_intercept_sessions[connection.initiator_unified_msg_origin] = connection_id
        self.message_intercept_sessions[connection.receiver_unified_msg_origin] = connection_id
        
        # 通知用户客服已接入
        await self.message_forwarder.notify_connection_established(connection)
        
        # 向管理员显示会话信息
        session_info = (
            f"✅ 成功接入客服会话\n"
            f"会话ID: {connection.connection_id}\n"
            f"用户ID: {connection.initiator_id}\n"
            f"创建时间: {connection.created_at.strftime('%Y-%m-%d %H:%M:%S')}\n"
        )
        
        if connection.initiator_description:
            session_info += f"问题描述: {connection.initiator_description}\n"
        
        session_info += f"\n💡 现在您可以直接发送消息与用户交流\n使用 连接状态 查看会话详情"
        
        yield event.plain_result(session_info)
        
        # 启动连接等待
        await self._start_connection_wait(event, connection_id)
    
    @filter.command("结束连线")
    async def end_connection(self, event: AstrMessageEvent):
        """结束当前连接"""
        connection = self.connection_manager.get_connection_by_user(event.unified_msg_origin)
        if not connection:
            yield event.plain_result("您当前没有活跃的连接")
            return
        
        # 通知对方连接已结束
        ended_by = str(event.get_sender_name())
        await self.message_forwarder.notify_connection_ended(connection, ended_by)
        
        # 清理消息拦截状态
        self.message_intercept_sessions.pop(connection.initiator_unified_msg_origin, None)
        if connection.receiver_unified_msg_origin:
            self.message_intercept_sessions.pop(connection.receiver_unified_msg_origin, None)
        
        # 结束连接
        await self.connection_manager.end_connection(connection.connection_id)
        
        yield event.plain_result("✅ 已结束连接")
    
    @filter.command("连接状态")
    async def connection_status(self, event: AstrMessageEvent):
        """查看连接状态"""
        user_id = str(event.get_sender_id())
        is_admin = event.is_admin()
        
        if is_admin:
            # 管理员查看所有连接状态
            active_connections = await self.connection_manager.get_active_connections()
            
            if not active_connections:
                yield event.plain_result("当前没有活跃的连接")
                return
            
            # 构建状态信息
            status_msg = "📊 连接系统状态\n\n"
            
            user_connections = [c for c in active_connections if c.connection_type == ConnectionType.USER_TO_USER]
            cs_connections = [c for c in active_connections if c.connection_type == ConnectionType.CUSTOMER_SERVICE]
            
            if user_connections:
                status_msg += f"👥 用户连线 ({len(user_connections)}):\n"
                for conn in user_connections:
                    status_text = "🔗 已连接" if conn.status == ConnectionStatus.CONNECTED else "⏳ 等待中"
                    status_msg += f"• {conn.connection_id} - {conn.initiator_id} ↔ {conn.receiver_id or '等待'} {status_text}\n"
                status_msg += "\n"
            
            if cs_connections:
                waiting_cs = [c for c in cs_connections if c.status == ConnectionStatus.REQUESTING]
                connected_cs = [c for c in cs_connections if c.status == ConnectionStatus.CONNECTED]
                
                if waiting_cs:
                    status_msg += f"⏳ 等待客服接入 ({len(waiting_cs)}):\n"
                    for conn in waiting_cs:
                        from datetime import datetime
                        elapsed = (datetime.now() - conn.created_at).total_seconds() / 60
                        status_msg += f"• {conn.connection_id} - 用户{conn.initiator_id} (等待{int(elapsed)}分钟)\n"
                    status_msg += "\n"
                
                if connected_cs:
                    status_msg += f"✅ 进行中的客服会话 ({len(connected_cs)}):\n"
                    for conn in connected_cs:
                        duration = ""
                        if conn.connected_at:
                            from datetime import datetime
                            elapsed = (datetime.now() - conn.connected_at).total_seconds() / 60
                            duration = f" (进行{int(elapsed)}分钟)"
                        status_msg += f"• {conn.connection_id} - 用户{conn.initiator_id} - 客服{conn.receiver_id}{duration}\n"
            
            # 当前管理员状态
            current_connection = self.connection_manager.get_connection_by_user(event.unified_msg_origin)
            if current_connection:
                status_msg += f"\n👤 您当前接入连接: {current_connection.connection_id}"
            else:
                status_msg += f"\n👤 您当前未接入任何连接"
            
            yield event.plain_result(status_msg)
        else:
            # 普通用户查看自己的连接状态
            connection = self.connection_manager.get_connection_by_user(event.unified_msg_origin)
            if not connection:
                yield event.plain_result("您当前没有活跃的连接")
                return
            
            if connection.connection_type == ConnectionType.USER_TO_USER:
                other_user = connection.receiver_id if connection.receiver_id else "等待中"
                status_msg = f"🔗 用户连线状态\n连接ID: {connection.connection_id}\n对方用户: {other_user}"
            else:
                cs_status = "客服已接入" if connection.status == ConnectionStatus.CONNECTED else "等待客服接入"
                status_msg = f"🛠️ 客服连接状态\n会话ID: {connection.connection_id}\n状态: {cs_status}"
            
            yield event.plain_result(status_msg)
    
    def get_session_id(self, event: AstrMessageEvent):
        return event.unified_msg_origin
    
    async def _start_connection_wait(self, event: AstrMessageEvent, connection_id: str):
        """启动连接等待"""
        @session_waiter(timeout=3600, record_history_chains=False)  # 60分钟超时
        async def connection_handler(controller: SessionController, conn_event: AstrMessageEvent):
            connection = self.connection_manager.get_connection(connection_id)
            if not connection or not connection.is_active():
                controller.stop()
                return
            
            # 检查是否是系统命令
            message_content = self.message_forwarder._extract_message_content(conn_event)
            if message_content == "结束连线":
                async for ret in self.end_connection(conn_event):
                    await conn_event.send(ret)
                return
            
            # 如果连接已建立，转发消息
            if connection.is_connected():
                await self.message_forwarder.forward_message(connection, conn_event)
                # 延长连接时间
                connection.extend_timeout(60)
                controller.keep(timeout=3600, reset_timeout=True)
            else:
                # 仍在等待连接
                if connection.connection_type == ConnectionType.USER_TO_USER:
                    await conn_event.send(
                        conn_event.plain_result(f"正在等待对方接受连线请求 (连接ID: {connection_id})...")
                    )
                else:
                    await conn_event.send(
                        conn_event.plain_result(f"正在等待客服接入 (会话ID: {connection_id})...")
                    )
                controller.keep(timeout=3600, reset_timeout=True)
            
            conn_event.stop_event()
        
        try:
            await connection_handler(event, session_filter=PersonSessionFilter(self.get_session_id(event)))
        except TimeoutError:
            # 连接超时，自动结束
            await self.connection_manager.end_connection(connection_id)
            # 清理拦截状态
            connection = self.connection_manager.get_connection(connection_id)
            if connection:
                self.message_intercept_sessions.pop(connection.initiator_unified_msg_origin, None)
                if connection.receiver_unified_msg_origin:
                    self.message_intercept_sessions.pop(connection.receiver_unified_msg_origin, None)
        except Exception as e:
            logger.error(f"Error in connection handler: {e}")
        finally:
            event.stop_event()
    
    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def intercept_messages(self, event: AstrMessageEvent):
        """拦截连接中的消息"""
        user_origin = event.unified_msg_origin
        
        # 检查用户是否有活跃的连接
        if user_origin not in self.message_intercept_sessions:
            return
        
        connection_id = self.message_intercept_sessions[user_origin]
        connection = self.connection_manager.get_connection(connection_id)
        
        if not connection or not connection.is_active() or not connection.is_connected():
            # 清理无效的拦截状态
            self.message_intercept_sessions.pop(user_origin, None)
            return
        
        # 检查消息内容
        message_content = self.message_forwarder._extract_message_content(event)
        
        # 如果是系统命令，不拦截
        if message_content.startswith('/'):
            return
        
        # 转发消息给对方
        await self.message_forwarder.forward_message(connection, event)
        
        # 阻止消息继续传播
        event.stop_event()

class PersonSessionFilter(SessionFilter):
    """会话过滤器，确保每个群的会话独立"""

    def __init__(self, session_id: str):
        # 为每个会话保存其所属群 ID
        self.session_id = session_id

    def filter(self, event: AstrMessageEvent) -> str:
        sender_id = event.unified_msg_origin
        # 仅当事件来自该群时才返回有效的会话 ID，否则返回空串避免误触发
        return self.session_id if sender_id == self.session_id else ""