"""
客服会话管理模块
"""

import time
import uuid
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, Optional, List
from dataclasses import dataclass, field
from astrbot.core.platform import AstrMessageEvent


class SessionStatus(Enum):
    """客服会话状态"""
    WAITING_FOR_ADMIN = "waiting_for_admin"
    ADMIN_CONNECTED = "admin_connected"
    ENDED = "ended"


@dataclass
class CustomerServiceSession:
    """客服会话数据模型"""
    session_id: str
    user_id: str
    user_unified_msg_origin: str
    user_description: Optional[str] = None
    admin_id: Optional[str] = None
    admin_unified_msg_origin: Optional[str] = None
    status: SessionStatus = SessionStatus.WAITING_FOR_ADMIN
    created_at: datetime = field(default_factory=datetime.now)
    connected_at: Optional[datetime] = None
    timeout_at: Optional[datetime] = None
    message_history: List[str] = field(default_factory=list)
    
    def __post_init__(self):
        """初始化后设置超时时间"""
        if self.timeout_at is None:
            self.timeout_at = self.created_at + timedelta(minutes=30)
    
    def stop(self):
        """结束会话"""
        self.status = SessionStatus.ENDED
    
    def connect_admin(self, admin_id: str, admin_unified_msg_origin: str):
        """连接管理员"""
        self.admin_id = admin_id
        self.admin_unified_msg_origin = admin_unified_msg_origin
        self.status = SessionStatus.ADMIN_CONNECTED
        self.connected_at = datetime.now()
        # 连接后延长超时时间
        self.extend_timeout(60)  # 延长60分钟
    
    def is_active(self) -> bool:
        """检查会话是否仍然活跃"""
        if self.status == SessionStatus.ENDED:
            return False
        if self.timeout_at and datetime.now() > self.timeout_at:
            self.status = SessionStatus.ENDED
            return False
        return True
    
    def extend_timeout(self, minutes: int):
        """延长超时时间"""
        if self.timeout_at:
            self.timeout_at = datetime.now() + timedelta(minutes=minutes)
    
    def is_admin_connected(self) -> bool:
        """检查管理员是否已连接"""
        return self.status == SessionStatus.ADMIN_CONNECTED and self.admin_id is not None
    
    def add_message_to_history(self, message: str):
        """添加消息到历史记录"""
        self.message_history.append(f"{datetime.now().strftime('%H:%M:%S')} - {message}")


class SessionManager:
    """客服会话管理器"""
    
    def __init__(self):
        self.active_sessions: Dict[str, CustomerServiceSession] = {}
        self.user_sessions: Dict[str, str] = {}  # user_unified_msg_origin -> session_id
        self.admin_sessions: Dict[str, str] = {}  # admin_id -> session_id
        
    async def create_session(self, user_event: AstrMessageEvent, description: str = None) -> str:
        """创建新的客服会话"""
        # 检查用户是否已有活跃会话
        user_origin = user_event.unified_msg_origin
        if user_origin in self.user_sessions:
            existing_session_id = self.user_sessions[user_origin]
            existing_session = self.active_sessions.get(existing_session_id)
            if existing_session and existing_session.is_active():
                return existing_session_id
            else:
                # 清理过期会话
                await self._cleanup_session(existing_session_id)
        
        # 创建新会话
        session_id = str(uuid.uuid4())[:8]  # 使用短ID方便管理员输入
        user_id = str(user_event.get_sender_id())
        
        session = CustomerServiceSession(
            session_id=session_id,
            user_id=user_id,
            user_unified_msg_origin=user_origin,
            user_description=description
        )
        
        self.active_sessions[session_id] = session
        self.user_sessions[user_origin] = session_id
        
        return session_id
    
    async def connect_admin(self, session_id: str, admin_event: AstrMessageEvent) -> bool:
        """管理员连接到会话"""
        session = self.active_sessions.get(session_id)
        if not session or not session.is_active():
            return False
            
        if session.status != SessionStatus.WAITING_FOR_ADMIN:
            return False
            
        admin_id = str(admin_event.get_sender_id())
        admin_origin = admin_event.unified_msg_origin
        
        # 检查管理员是否已有其他活跃会话
        if admin_id in self.admin_sessions:
            existing_session_id = self.admin_sessions[admin_id]
            existing_session = self.active_sessions.get(existing_session_id)
            if existing_session and existing_session.is_active():
                return False  # 管理员已有活跃会话
        
        session.connect_admin(admin_id, admin_origin)
        self.admin_sessions[admin_id] = session_id
        
        return True
    
    async def end_session(self, session_id: str) -> bool:
        """结束客服会话"""
        session = self.active_sessions.get(session_id)
        if not session:
            return False
            
        session.stop()
        await self._cleanup_session(session_id)
        return True
        
    async def get_session_by_user(self, user_unified_msg_origin: str) -> Optional[CustomerServiceSession]:
        """根据用户获取活跃会话"""
        session_id = self.user_sessions.get(user_unified_msg_origin)
        if session_id:
            session = self.active_sessions.get(session_id)
            if session and session.is_active():
                return session
            else:
                # 清理过期会话
                await self._cleanup_session(session_id)
        return None
    
    async def get_session_by_admin(self, admin_id: str) -> Optional[CustomerServiceSession]:
        """根据管理员获取活跃会话"""
        session_id = self.admin_sessions.get(admin_id)
        if session_id:
            session = self.active_sessions.get(session_id)
            if session and session.is_active():
                return session
            else:
                # 清理过期会话
                await self._cleanup_session(session_id)
        return None
    
    def get_session(self, session_id: str) -> Optional[CustomerServiceSession]:
        """根据会话ID获取会话"""
        session = self.active_sessions.get(session_id)
        if session and session.is_active():
            return session
        return None
    
    async def get_active_sessions(self) -> List[CustomerServiceSession]:
        """获取所有活跃会话"""
        active = []
        for session in list(self.active_sessions.values()):
            if session.is_active():
                active.append(session)
            else:
                # 清理过期会话
                await self._cleanup_session(session.session_id)
        return active
    
    async def cleanup_expired_sessions(self):
        """清理过期会话"""
        expired_sessions = []
        for session_id, session in list(self.active_sessions.items()):
            if not session.is_active():
                expired_sessions.append(session_id)
        
        for session_id in expired_sessions:
            await self._cleanup_session(session_id)
    
    async def _cleanup_session(self, session_id: str):
        """清理会话相关数据"""
        session = self.active_sessions.pop(session_id, None)
        if session:
            # 清理用户会话映射
            self.user_sessions.pop(session.user_unified_msg_origin, None)
            # 清理管理员会话映射
            if session.admin_id:
                self.admin_sessions.pop(session.admin_id, None)