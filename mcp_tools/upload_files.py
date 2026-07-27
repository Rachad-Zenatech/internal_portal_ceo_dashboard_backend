import json

async def list_uploaded_files_tool(upload_type: str = None) -> str:
    """
    Get a list of all uploaded archive files.
    
    Args:
        upload_type (str, optional): Filter by upload type ('general-ledger' or 'bank-statement').
        
    Returns:
        str: JSON string containing a list of uploaded files.
    """
    try:
        from services.upload_archive_service import list_uploads
        res = list_uploads(upload_type=upload_type)
        return json.dumps(res, indent=2)
    except Exception as e:
        return f"Error fetching uploaded files: {e}"

async def get_uploaded_file_tool(file_id: str) -> str:
    """
    Get metadata for a specific uploaded archive file by ID.
    
    Args:
        file_id (str): The ID of the uploaded file.
        
    Returns:
        str: JSON string containing the file metadata.
    """
    try:
        from services.upload_archive_service import get_upload
        res = get_upload(file_id)
        return json.dumps(res, indent=2)
    except Exception as e:
        return f"Error fetching uploaded file {file_id}: {e}"
