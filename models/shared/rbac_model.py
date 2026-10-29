from pydantic import BaseModel, ConfigDict, EmailStr
from typing import Optional, List, Dict, Any
from datetime import datetime
from uuid import UUID

# Users
class UserBase(BaseModel):
    email: EmailStr
    full_name: Optional[str] = None
    is_active: bool = True
    is_super_admin: bool = False

class UserCreate(UserBase):
    model_config = ConfigDict(extra="forbid")

class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: Optional[EmailStr] = None
    full_name: Optional[str] = None
    is_active: Optional[bool] = None
    is_super_admin: Optional[bool] = None

class User(UserBase):
    id: UUID
    last_login_at: Optional[datetime] = None
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True

# Roles
class RoleBase(BaseModel):
    name: str
    code: str
    description: Optional[str] = None
    is_system_role: bool = False
    is_active: bool = True
    parent_role_id: Optional[UUID] = None
    display_order: int = 0
    department: Optional[str] = None

class RoleCreate(RoleBase):
    pass

class RoleUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    is_active: Optional[bool] = None
    parent_role_id: Optional[UUID] = None
    display_order: Optional[int] = None
    department: Optional[str] = None

class Role(RoleBase):
    id: UUID
    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True

class RoleTree(Role):
    level: int = 0
    children: List['RoleTree'] = []
    
    class Config:
        from_attributes = True

# User Roles
class UserRoleBase(BaseModel):
    user_id: UUID
    role_id: UUID
    is_active: bool = True

class UserRole(UserRoleBase):
    id: UUID
    assigned_at: datetime
    assigned_by: Optional[UUID] = None
    
    class Config:
        from_attributes = True

# Navigation Items (Pages)
class NavigationItem(BaseModel):
    id: UUID
    name: str
    code: str
    route_path: Optional[str] = None
    parent_code: Optional[str] = None
    display_order: int
    icon: Optional[str] = None
    is_menu_item: bool
    is_active: bool

    class Config:
        from_attributes = True

# Permission Actions
class PermissionAction(BaseModel):
    id: UUID
    name: str
    code: str
    description: Optional[str] = None

    class Config:
        from_attributes = True

# Role Navigation Permissions
class RoleNavigationPermissionCreate(BaseModel):
    navigation_item_id: UUID
    action_id: UUID
    is_allowed: bool

class RoleNavigationPermissionUpdate(BaseModel):
    is_allowed: bool

class RoleNavigationPermission(BaseModel):
    id: UUID
    role_id: UUID
    navigation_item_id: UUID
    action_id: UUID
    is_allowed: bool
    
    # Extra fields for ease of use in responses
    navigation_code: Optional[str] = None
    action_code: Optional[str] = None

    class Config:
        from_attributes = True

# MCP Tools
class McpTool(BaseModel):
    id: UUID
    name: str
    code: str
    server_name: str
    description: Optional[str] = None
    is_read_only: bool
    is_sensitive: bool
    is_active: bool

    class Config:
        from_attributes = True

# Role MCP Tool Permissions
class RoleMcpToolPermissionCreate(BaseModel):
    mcp_tool_id: UUID
    is_allowed: bool
    access_level: str = "ALLOW"
    conditions: Optional[Dict[str, Any]] = None

class RoleMcpToolPermissionUpdate(BaseModel):
    is_allowed: Optional[bool] = None
    access_level: Optional[str] = None
    conditions: Optional[Dict[str, Any]] = None

class RoleMcpToolPermission(BaseModel):
    id: UUID
    role_id: UUID
    mcp_tool_id: UUID
    is_allowed: bool
    access_level: str
    conditions: Optional[Dict[str, Any]] = None
    
    # Extra field for ease of use
    tool_code: Optional[str] = None

    class Config:
        from_attributes = True

# My Permissions Response
class MyPermissionsResponse(BaseModel):
    user: User
    roles: List[Role]
    navigation_permissions: Dict[str, List[str]] # e.g. {"DASHBOARD": ["VIEW", "EDIT"]}
    mcp_tool_permissions: List[str] # List of allowed tool codes

# PBAC Models
class PermissionGroupAction(BaseModel):
    api_module_code: str
    action: str
    
    class Config:
        from_attributes = True

class PermissionGroup(BaseModel):
    id: int
    code: str
    name: str
    description: Optional[str] = None
    actions: List[PermissionGroupAction] = []
    
    class Config:
        from_attributes = True

class PermissionModule(BaseModel):
    code: str
    name: str
    groups: List[PermissionGroup] = []
    
    class Config:
        from_attributes = True

class RolePermissionGroupsUpdate(BaseModel):
    permission_group_ids: List[int]

