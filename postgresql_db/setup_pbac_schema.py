import asyncio
import os
import sys
from dotenv import load_dotenv
import uuid
from postgresql_db.database import create_pool, close_pool, get_pool

# Load .env
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))

async def setup():
    await create_pool()
    pool = get_pool()
    conn = await pool.acquire()
    
    try:
        print("Creating tables...")
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS permission_modules (
                id SERIAL PRIMARY KEY,
                code VARCHAR(255) UNIQUE NOT NULL,
                name VARCHAR(255) NOT NULL,
                sort_order INT NOT NULL DEFAULT 0,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS permission_groups (
                id SERIAL PRIMARY KEY,
                module_id INT REFERENCES permission_modules(id) ON DELETE CASCADE,
                code VARCHAR(255) UNIQUE NOT NULL,
                name VARCHAR(255) NOT NULL,
                description TEXT,
                sort_order INT NOT NULL DEFAULT 0,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS permission_group_actions (
                id SERIAL PRIMARY KEY,
                permission_group_id INT REFERENCES permission_groups(id) ON DELETE CASCADE,
                api_module_code VARCHAR(255) NOT NULL,
                action VARCHAR(255) NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS role_permission_groups (
                id SERIAL PRIMARY KEY,
                role_id UUID NOT NULL,
                permission_group_id INT REFERENCES permission_groups(id) ON DELETE CASCADE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                UNIQUE(role_id, permission_group_id)
            );
        """)
        
        print("Clearing old data...")
        await conn.execute("TRUNCATE permission_modules CASCADE")
        
        print("Seeding permission modules...")
        modules = [
            ("REPORTS", "Reports & Dashboard", 40),
            ("USER_MANAGEMENT", "User Management", 50),
            ("FILES", "File Uploads", 60)
        ]
        
        for m in modules:
            await conn.execute("INSERT INTO permission_modules (code, name, sort_order) VALUES ($1, $2, $3)", *m)
            
        print("Seeding permission groups and actions...")
        
        groups_data = {

            "REPORTS": [
                ("REPORTS_VIEW_DASHBOARD", "View Dashboard", "Access the main dashboard metrics.", 10, [
                    ("DASHBOARD", "VIEW")
                ]),
                ("REPORTS_VIEW_REPORTS", "View Reports", "Access system generated reports.", 20, [
                    ("REPORTS", "VIEW")
                ]),
                ("REPORTS_VIEW_TRIAL_BALANCE", "View Trial Balance", "Access trial balance.", 30, [
                    ("TRIAL_BALANCE", "VIEW")
                ]),
                ("REPORTS_VIEW_CONSOLIDATED", "View Consolidated Reports", "Access consolidated matrix reports.", 40, [
                    ("CONSOLIDATED_TRIAL_BALANCE", "VIEW"), ("CONSOLIDATED_TRIAL_BALANCE_MATRIX", "VIEW")
                ])
            ],
            "USER_MANAGEMENT": [
                ("USER_MANAGEMENT_VIEW", "View Users & Roles", "View all users and role definitions.", 10, [
                    ("CONFIG_USERS", "VIEW"), ("CONFIG_ROLES", "VIEW"), ("CONFIG_USER_ROLE_ASSIGNMENT", "VIEW")
                ]),
                ("USER_MANAGEMENT_MANAGE_USERS", "Manage Users", "Create, edit, or delete system users.", 20, [
                    ("CONFIG_USERS", "CREATE"), ("CONFIG_USERS", "UPDATE"), ("CONFIG_USERS", "DELETE")
                ]),
                ("USER_MANAGEMENT_MANAGE_ROLES", "Manage Roles", "Create, edit, or delete role definitions.", 30, [
                    ("CONFIG_ROLES", "CREATE"), ("CONFIG_ROLES", "UPDATE"), ("CONFIG_ROLES", "DELETE")
                ]),
                ("USER_MANAGEMENT_ASSIGN_ROLES", "Assign Roles", "Assign roles to users.", 40, [
                    ("CONFIG_USER_ROLE_ASSIGNMENT", "CREATE"), ("CONFIG_USER_ROLE_ASSIGNMENT", "UPDATE"), ("CONFIG_USER_ROLE_ASSIGNMENT", "DELETE")
                ])
            ],
            "FILES": [
                ("FILES_VIEW", "View Uploaded Files", "View all uploaded files.", 10, [
                    ("UPLOAD_FILES", "VIEW")
                ]),
                ("FILES_UPLOAD", "Upload Files", "Upload files manually.", 20, [
                    ("UPLOAD_FILES", "CREATE")
                ]),
                ("FILES_DELETE", "Delete Uploaded Files", "Delete existing uploaded files.", 30, [
                    ("UPLOAD_FILES", "DELETE")
                ])
            ]
        }
        
        for module_code, groups in groups_data.items():
            module_id = await conn.fetchval("SELECT id FROM permission_modules WHERE code = $1", module_code)
            
            for g_code, g_name, g_desc, g_sort, actions in groups:
                g_id = await conn.fetchval("""
                    INSERT INTO permission_groups (module_id, code, name, description, sort_order)
                    VALUES ($1, $2, $3, $4, $5) RETURNING id
                """, module_id, g_code, g_name, g_desc, g_sort)
                
                for nav_code, act_code in actions:
                    await conn.execute("""
                        INSERT INTO permission_group_actions (permission_group_id, api_module_code, action)
                        VALUES ($1, $2, $3)
                    """, g_id, nav_code, act_code)
                    
        print("Migrating existing roles...")
        
        roles = await conn.fetch("SELECT id, code FROM roles")
        
        for r in roles:
            role_id = r['id']
            
            # Super admin gets everything
            if r['code'] == 'SUPER_ADMIN':
                await conn.execute("""
                    INSERT INTO role_permission_groups (role_id, permission_group_id)
                    SELECT $1, id FROM permission_groups
                    ON CONFLICT DO NOTHING
                """, role_id)
                print(f"Assigned all groups to SUPER_ADMIN ({role_id})")
                continue
            
            # Check existing permissions
            old_perms = await conn.fetch("""
                SELECT n.code as nav_code, a.code as act_code
                FROM role_navigation_permissions rnp
                JOIN navigation_items n ON rnp.navigation_item_id = n.id
                JOIN permission_actions a ON rnp.action_id = a.id
                WHERE rnp.role_id = $1 AND rnp.is_allowed = true
            """, role_id)
            
            old_perms_set = {(p['nav_code'], p['act_code']) for p in old_perms}
            
            # Check all groups
            all_groups = await conn.fetch("SELECT id FROM permission_groups")
            for g in all_groups:
                g_id = g['id']
                group_actions = await conn.fetch("SELECT api_module_code, action FROM permission_group_actions WHERE permission_group_id = $1", g_id)
                
                group_actions_set = {(ga['api_module_code'], ga['action']) for ga in group_actions}
                
                # If role has ALL actions in this group, assign the group!
                if group_actions_set.issubset(old_perms_set) and len(group_actions_set) > 0:
                    await conn.execute("""
                        INSERT INTO role_permission_groups (role_id, permission_group_id)
                        VALUES ($1, $2)
                        ON CONFLICT DO NOTHING
                    """, role_id, g_id)
                    
        print("Seeding super admins...")
        super_admins = [
            "ali.hassan.sharif@zenatech.com",
            "alvin.tsang@zenatech.com",
            "rachad.quintyne@zenatech.com",
            "ibraheem.suleiman@zenatech.com"
        ]
        
        # Ensure SUPER_ADMIN role exists
        await conn.execute("INSERT INTO roles (code, name, is_system_role) VALUES ('SUPER_ADMIN', 'Super Admin', true) ON CONFLICT (code) DO NOTHING")
        
        for email in super_admins:
            await conn.execute("""
                INSERT INTO users (email, full_name, is_active, is_super_admin)
                VALUES ($1, $2, true, true)
                ON CONFLICT (email) DO UPDATE SET is_super_admin = true
            """, email, email.split('@')[0].replace('.', ' ').title())

        print("Schema setup and migration complete!")
    finally:
        await pool.release(conn)
        await close_pool()

if __name__ == "__main__":
    asyncio.run(setup())
