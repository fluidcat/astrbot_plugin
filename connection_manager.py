"""
通用连接管理器 - 支持用户间连线和客服连接
"""

import time
import uuid
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, Optional, List, Tuple
from dataclasses import dataclass, field
from astrbot.core.platform import AstrMessageEvent
from astrbot.core.utils.session_waiter import (
    USER_SESSIONS
)


class ConnectionType(Enum):
    """连接类型"""
    USER_TO_USER = "user_to_user"      # 用户间连线
    CUSTOMER_SERVICE = "customer_service"  # 客服连接


class ConnectionStatus(Enum):
    """连接状态"""
    REQUESTING = "requesting"           # 请求中（等待对方同意）
    CONNECTED = "connected"            # 已连接
    ENDED = "ended"                    # 已结束


@dataclass
class Connection:
    """连接数据模型"""
    connection_id: str
    connection_type: ConnectionType
    
    # 发起方信息
    initiator_id: str
    initiator_name: str
    initiator_unified_msg_origin: str
    initiator_description: Optional[str] = None
    
    # 接收方信息
    receiver_id: Optional[str] = None
    receiver_name: Optional[str] = None
    receiver_unified_msg_origin: Optional[str] = None
    
    # 连接状态和时间
    status: ConnectionStatus = ConnectionStatus.REQUESTING
    created_at: datetime = field(default_factory=datetime.now)
    connected_at: Optional[datetime] = None
    timeout_at: Optional[datetime] = None
    
    # 消息历史
    message_history: List[str] = field(default_factory=list)
    
    def __post_init__(self):
        """初始化后设置超时时间"""
        if self.timeout_at is None:
            # 请求阶段30分钟超时，连接后60分钟超时
            timeout_minutes = 30 if self.status == ConnectionStatus.REQUESTING else 60
            self.timeout_at = self.created_at + timedelta(minutes=timeout_minutes)
    
    def connect(self, receiver_id: str, receiver_name: str, receiver_unified_msg_origin: str):
        """建立连接"""
        self.receiver_id = receiver_id
        self.receiver_name = receiver_name
        self.receiver_unified_msg_origin = receiver_unified_msg_origin
        self.status = ConnectionStatus.CONNECTED
        self.connected_at = datetime.now()
        # 连接后延长超时时间到60分钟
        self.extend_timeout(60)
    
    def end(self):
        """结束连接"""
        self.status = ConnectionStatus.ENDED
    
    def is_active(self) -> bool:
        """检查连接是否仍然活跃"""
        if self.status == ConnectionStatus.ENDED:
            return False
        if self.timeout_at and datetime.now() > self.timeout_at:
            self.status = ConnectionStatus.ENDED
            return False
        return True
    
    def extend_timeout(self, minutes: int):
        """延长超时时间"""
        self.timeout_at = datetime.now() + timedelta(minutes=minutes)
    
    def is_connected(self) -> bool:
        """检查是否已连接"""
        return (self.status == ConnectionStatus.CONNECTED and 
                self.receiver_id is not None)
    
    def add_message_to_history(self, sender: str, message: str):
        """添加消息到历史记录"""
        timestamp = datetime.now().strftime('%H:%M:%S')
        self.message_history.append(f"{timestamp} [{sender}] {message}")
    
    def get_other_party(self, user_unified_msg_origin: str) -> Optional[Tuple[str, str]]:
        """获取对方的信息 (user_id, unified_msg_origin)"""
        if user_unified_msg_origin == self.initiator_unified_msg_origin:
            return (self.receiver_id, self.receiver_unified_msg_origin)
        elif user_unified_msg_origin == self.receiver_unified_msg_origin:
            return (self.initiator_id, self.initiator_unified_msg_origin)
        return None


class ConnectionManager:
    """通用连接管理器"""
    
    def __init__(self):
        self.active_connections: Dict[str, Connection] = {}
        self.user_connections: Dict[str, str] = {}  # user_unified_msg_origin -> connection_id
        self.pending_requests: Dict[str, List[str]] = {}  # receiver_id -> [connection_ids]
        
    async def create_connection(self, connection_type: ConnectionType, 
                               initiator_event: AstrMessageEvent, 
                               receiver_id: str = None, 
                               description: str = None) -> str:
        """创建新连接"""
        # 检查发起方是否已有活跃连接
        initiator_origin = initiator_event.unified_msg_origin
        if initiator_origin in self.user_connections:
            existing_connection_id = self.user_connections[initiator_origin]
            existing_connection = self.active_connections.get(existing_connection_id)
            if existing_connection and existing_connection.is_active():
                return existing_connection_id
            else:
                # 清理过期连接
                await self._cleanup_connection(existing_connection_id)
        
        # 创建新连接
        connection_id = str(uuid.uuid4())[:8]  # 使用短ID方便输入
        initiator_id = str(initiator_event.get_sender_id())
        initiator_name = str(initiator_event.get_sender_name())
        
        connection = Connection(
            connection_id=connection_id,
            connection_type=connection_type,
            initiator_id=initiator_id,
            initiator_name=initiator_name,
            initiator_unified_msg_origin=initiator_origin,
            initiator_description=description
        )
        
        self.active_connections[connection_id] = connection
        self.user_connections[initiator_origin] = connection_id
        
        # 如果是用户间连线，添加到待处理请求
        if connection_type == ConnectionType.USER_TO_USER and receiver_id:
            if receiver_id not in self.pending_requests:
                self.pending_requests[receiver_id] = []
            self.pending_requests[receiver_id].append(connection_id)
        
        return connection_id
    
    async def accept_connection(self, connection_id: str, receiver_event: AstrMessageEvent) -> bool:
        """接受连接"""
        connection = self.active_connections.get(connection_id)
        if not connection or not connection.is_active():
            return False
            
        if connection.status != ConnectionStatus.REQUESTING:
            return False
            
        receiver_id = str(receiver_event.get_sender_id())
        receiver_name = receiver_event.get_sender_name()
        receiver_origin = receiver_event.unified_msg_origin
        
        # 检查接收方是否已有其他活跃连接
        if receiver_origin in self.user_connections:
            existing_connection_id = self.user_connections[receiver_origin]
            existing_connection = self.active_connections.get(existing_connection_id)
            if existing_connection and existing_connection.is_active():
                return False  # 接收方已有活跃连接
        
        connection.connect(receiver_id, receiver_name, receiver_origin)
        self.user_connections[receiver_origin] = connection_id
        
        # 清理待处理请求
        if receiver_id in self.pending_requests:
            if connection_id in self.pending_requests[receiver_id]:
                self.pending_requests[receiver_id].remove(connection_id)
            if not self.pending_requests[receiver_id]:
                del self.pending_requests[receiver_id]
        
        return True
    
    async def end_connection(self, connection_id: str) -> bool:
        """结束连接"""
        connection = self.active_connections.get(connection_id)
        if not connection:
            return False

        if session_waiter:=USER_SESSIONS.get(connection.initiator_unified_msg_origin):
            session_waiter.session_controller.stop()
        if session_waiter:=USER_SESSIONS.get(connection.receiver_unified_msg_origin):
            session_waiter.session_controller.stop()

        connection.end()
        await self._cleanup_connection(connection_id)
        return True
    
    def get_connection_by_user(self, user_unified_msg_origin: str) -> Optional[Connection]:
        """根据用户获取活跃连接"""
        connection_id = self.user_connections.get(user_unified_msg_origin)
        if connection_id:
            connection = self.active_connections.get(connection_id)
            if connection and connection.is_active():
                return connection
            else:
                # 异步清理过期连接
                import asyncio
                asyncio.create_task(self._cleanup_connection(connection_id))
        return None
    
    def get_connection(self, connection_id: str) -> Optional[Connection]:
        """根据连接ID获取连接"""
        connection = self.active_connections.get(connection_id)
        if connection and connection.is_active():
            return connection
        return None
    
    async def get_active_connections(self) -> List[Connection]:
        """获取所有活跃连接"""
        active = []
        for connection in list(self.active_connections.values()):
            if connection.is_active():
                active.append(connection)
            else:
                # 清理过期连接
                await self._cleanup_connection(connection.connection_id)
        return active
    
    def get_pending_requests_for_user(self, user_id: str) -> List[Connection]:
        """获取指定用户的待处理连接请求"""
        connection_ids = self.pending_requests.get(user_id, [])
        connections = []
        for connection_id in connection_ids:
            connection = self.active_connections.get(connection_id)
            if connection and connection.is_active() and connection.status == ConnectionStatus.REQUESTING:
                connections.append(connection)
        return connections
    
    async def cleanup_expired_connections(self):
        """清理过期连接"""
        expired_connections = []
        for connection_id, connection in list(self.active_connections.items()):
            if not connection.is_active():
                expired_connections.append(connection_id)
        
        for connection_id in expired_connections:
            await self._cleanup_connection(connection_id)
    
    async def _cleanup_connection(self, connection_id: str):
        """清理连接相关数据"""
        connection = self.active_connections.pop(connection_id, None)
        if connection:
            # 清理用户连接映射
            self.user_connections.pop(connection.initiator_unified_msg_origin, None)
            if connection.receiver_unified_msg_origin:
                self.user_connections.pop(connection.receiver_unified_msg_origin, None)
            
            # 清理待处理请求
            for user_id, request_list in list(self.pending_requests.items()):
                if connection_id in request_list:
                    request_list.remove(connection_id)
                if not request_list:
                    del self.pending_requests[user_id]