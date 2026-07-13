"""
微信草稿 API Server
提供接口用于向微信公众号创建草稿
"""
from logging import log
import os
import time
import json
import asyncio
from typing import Optional, Dict, Any, Union
from io import BytesIO
from pathlib import Path
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
import httpx
from PIL import Image
from cozepy import AsyncCoze, AsyncTokenAuth, COZE_CN_BASE_URL, load_oauth_app_from_config, load_oauth_app_from_config
from uvicorn.main import logger

app = FastAPI(title="微信草稿 API", description="微信公众号草稿创建服务")
BASE_DIR = Path(__file__).resolve().parent

# Access token 缓存
_token_cache: Dict[str, Dict[str, Any]] = {}

# Coze OAuth 应用缓存
_coze_oauth_app: Optional[Any] = None

# Coze access token 缓存
_coze_token_cache: Optional[Dict[str, Any]] = None

WECHAT_TITLE_MAX_LENGTH = 64
WECHAT_AUTHOR_MAX_LENGTH = 16
WECHAT_DIGEST_MAX_LENGTH = 120
WECHAT_CONTENT_MAX_LENGTH = 20000
WECHAT_CONTENT_MAX_BYTES = 1024 * 1024
WECHAT_CONTENT_SOURCE_URL_MAX_LENGTH = 200


def validate_wechat_content(value: str) -> str:
    if len(value) > WECHAT_CONTENT_MAX_LENGTH:
        raise ValueError(f"content cannot exceed {WECHAT_CONTENT_MAX_LENGTH} characters")

    content_bytes = len(value.encode("utf-8"))
    if content_bytes > WECHAT_CONTENT_MAX_BYTES:
        raise ValueError(f"content cannot exceed {WECHAT_CONTENT_MAX_BYTES} bytes after UTF-8 encoding")

    return value


def resolve_wechat_credentials(app_id: Optional[str], app_secret: Optional[str]) -> tuple[str, str]:
    """
    优先使用请求里的公众号凭据；未传时读取环境变量。
    """
    resolved_app_id = app_id or os.getenv("WECHAT_APP_ID")
    resolved_app_secret = app_secret or os.getenv("WECHAT_APP_SECRET")

    if not resolved_app_id or not resolved_app_secret:
        raise HTTPException(
            status_code=400,
            detail="WeChat credentials are required. Provide app_id/app_secret or set WECHAT_APP_ID/WECHAT_APP_SECRET."
        )

    return resolved_app_id, resolved_app_secret


class ImageItem(BaseModel):
    """图片项"""
    image_media_id: str = Field(..., description="图片 media_id")


class ImageInfo(BaseModel):
    """图片信息"""
    image_list: list[ImageItem] = Field(..., description="图片列表，最多20张")


class CoverInfo(BaseModel):
    """封面信息"""
    crop_percent_list: Optional[list[Dict[str, Any]]] = Field(None, description="封面裁剪信息")


class FooterProductInfo(BaseModel):
    """文末商品信息"""
    product_key: Optional[str] = Field(None, description="商品key")


class ProductInfo(BaseModel):
    """商品信息"""
    footer_product_info: Optional[FooterProductInfo] = Field(None, description="文末插入商品相关信息")


class NewsDraftRequest(BaseModel):
    """图文消息草稿请求"""
    article_type: str = Field(default="news", description="文章类型")
    title: str = Field(..., min_length=1, max_length=WECHAT_TITLE_MAX_LENGTH, description="标题")
    content: str = Field(..., min_length=1, description="正文内容，支持HTML标签，小于20000字符且小于1MB")
    thumb_media_id: Optional[str] = Field(None, description="封面图片 media_id，必须是永久 MediaID。如果提供了 digest，可以自动生成")
    app_id: Optional[str] = Field(None, description="AppID，不传则读取 WECHAT_APP_ID")
    app_secret: Optional[str] = Field(None, description="AppSecret，不传则读取 WECHAT_APP_SECRET")
    author: Optional[str] = Field(None, max_length=WECHAT_AUTHOR_MAX_LENGTH, description="作者，最多16字符")
    digest: Optional[str] = Field(None, max_length=WECHAT_DIGEST_MAX_LENGTH, description="摘要，单图文才有，多图文为空。如果提供了 digest 但没有 thumb_media_id，将自动生成封面图")
    content_source_url: Optional[str] = Field(None, max_length=WECHAT_CONTENT_SOURCE_URL_MAX_LENGTH, description="原文链接")
    need_open_comment: int = Field(default=0, ge=0, le=1, description="是否打开评论，0-关闭，1-打开")
    only_fans_can_comment: int = Field(default=0, ge=0, le=1, description="是否只有粉丝可评论，0-所有人，1-仅粉丝")
    pic_crop_235_1: Optional[str] = Field(None, description="封面裁剪坐标 2.35:1")
    pic_crop_1_1: Optional[str] = Field(None, description="封面裁剪坐标 1:1")
    product_info: Optional[ProductInfo] = Field(None, description="商品信息")

    @field_validator("article_type")
    @classmethod
    def validate_article_type(cls, v):
        if v not in ["news", "newspic"]:
            raise ValueError('article_type must be "news" or "newspic"')
        return v

    @field_validator("content")
    @classmethod
    def validate_content(cls, v):
        return validate_wechat_content(v)

    @model_validator(mode="after")
    def validate_thumb_media_id(self):
        """验证：如果既没有 digest 也没有 thumb_media_id，则报错"""
        if not self.digest and not self.thumb_media_id:
            raise ValueError("Either 'digest' or 'thumb_media_id' must be provided")
        return self


class ImageUploadFromUrlRequest(BaseModel):
    """通过图片 URL 上传素材请求"""
    image_url: str = Field(..., description="图片 URL")
    app_id: Optional[str] = Field(None, description="AppID，不传则读取 WECHAT_APP_ID")
    app_secret: Optional[str] = Field(None, description="AppSecret，不传则读取 WECHAT_APP_SECRET")
    usage: str = Field(default="cover", description="用途：cover=封面永久素材，content=正文图片")

    @field_validator("usage")
    @classmethod
    def validate_usage(cls, v):
        if v not in ["cover", "content"]:
            raise ValueError('usage must be "cover" or "content"')
        return v


class ArticleDraftRequest(BaseModel):
    """完整公众号图文草稿请求，适合自动化发布流程"""
    title: str = Field(..., min_length=1, max_length=WECHAT_TITLE_MAX_LENGTH, description="标题")
    content: str = Field(..., min_length=1, description="已经排版好的公众号 HTML 正文")
    app_id: Optional[str] = Field(None, description="AppID，不传则读取 WECHAT_APP_ID")
    app_secret: Optional[str] = Field(None, description="AppSecret，不传则读取 WECHAT_APP_SECRET")
    thumb_media_id: Optional[str] = Field(None, description="封面永久素材 media_id")
    thumb_image_url: Optional[str] = Field(None, description="封面图片 URL，服务会上传为永久素材")
    author: Optional[str] = Field(None, max_length=WECHAT_AUTHOR_MAX_LENGTH, description="作者，最多16字符")
    digest: Optional[str] = Field(None, max_length=WECHAT_DIGEST_MAX_LENGTH, description="摘要，建议 120 字以内")
    content_source_url: Optional[str] = Field(None, max_length=WECHAT_CONTENT_SOURCE_URL_MAX_LENGTH, description="原文链接")
    need_open_comment: int = Field(default=0, ge=0, le=1, description="是否打开评论")
    only_fans_can_comment: int = Field(default=0, ge=0, le=1, description="是否只有粉丝可评论")
    pic_crop_235_1: Optional[str] = Field(None, description="封面裁剪坐标 2.35:1")
    pic_crop_1_1: Optional[str] = Field(None, description="封面裁剪坐标 1:1")
    publish: bool = Field(default=False, description="是否创建草稿后立即提交发布")

    @field_validator("content")
    @classmethod
    def validate_content(cls, v):
        return validate_wechat_content(v)


class PublishDraftRequest(BaseModel):
    """发布草稿请求"""
    media_id: str = Field(..., description="草稿 media_id")
    app_id: Optional[str] = Field(None, description="AppID，不传则读取 WECHAT_APP_ID")
    app_secret: Optional[str] = Field(None, description="AppSecret，不传则读取 WECHAT_APP_SECRET")


class NewspicDraftRequest(BaseModel):
    """图片消息草稿请求"""
    article_type: str = Field(default="newspic", description="文章类型")
    title: str = Field(..., min_length=1, max_length=WECHAT_TITLE_MAX_LENGTH, description="标题")
    content: str = Field(..., min_length=1, description="正文内容，支持HTML标签，小于20000字符且小于1MB")
    image_info: ImageInfo = Field(..., description="图片信息，最多20张图片，第一张为封面")
    app_id: Optional[str] = Field(None, description="AppID，不传则读取 WECHAT_APP_ID")
    app_secret: Optional[str] = Field(None, description="AppSecret，不传则读取 WECHAT_APP_SECRET")
    need_open_comment: int = Field(default=0, ge=0, le=1, description="是否打开评论，0-关闭，1-打开")
    only_fans_can_comment: int = Field(default=0, ge=0, le=1, description="是否只有粉丝可评论，0-所有人，1-仅粉丝")
    cover_info: Optional[CoverInfo] = Field(None, description="封面信息")
    product_info: Optional[ProductInfo] = Field(None, description="商品信息")

    @field_validator("article_type")
    @classmethod
    def validate_article_type(cls, v):
        if v != "newspic":
            raise ValueError('article_type must be "newspic"')
        return v

    @field_validator("content")
    @classmethod
    def validate_content(cls, v):
        return validate_wechat_content(v)

    @field_validator("image_info")
    @classmethod
    def validate_image_info(cls, v):
        if len(v.image_list) == 0:
            raise ValueError("image_list cannot be empty")
        if len(v.image_list) > 20:
            raise ValueError("image_list cannot exceed 20 images")
        return v


def load_coze_oauth_app(config_path: str) -> Any:
    """
    加载 Coze OAuth 应用配置
    
    Args:
        config_path: OAuth 配置文件路径
        
    Returns:
        OAuth 应用对象
        
    Raises:
        HTTPException: 当加载配置失败时
    """
    global _coze_oauth_app
    
    if _coze_oauth_app is not None:
        return _coze_oauth_app
    
    try:
        with open(config_path, "r", encoding="utf-8") as file:
            config = json.loads(file.read())
        _coze_oauth_app = load_oauth_app_from_config(config)
        return _coze_oauth_app
    except FileNotFoundError:
        raise HTTPException(
            status_code=500,
            detail=f"Coze OAuth configuration file not found: {config_path}. Please make sure you have created the OAuth configuration file."
        )
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to load Coze OAuth configuration: {str(e)}"
        )


async def get_coze_access_token() -> str:
    """
    获取 Coze access token，使用 OAuth 方式，带缓存机制
    
    Returns:
        access_token 字符串
        
    Raises:
        HTTPException: 当获取 token 失败时
    """
    global _coze_token_cache
    
    current_time = time.time()
    
    # 检查缓存，token 有效期通常较长，提前 300 秒刷新
    if _coze_token_cache is not None:
        cached_data = _coze_token_cache
        expires_in = cached_data.get("expires_in", 0)
        timestamp = cached_data.get("timestamp", 0)
        
        # 判断 expires_in 是时间戳（绝对时间）还是相对时间（秒数）
        if isinstance(expires_in, (int, float)):
            if expires_in > 1000000000:
                # 时间戳格式（绝对时间）
                if expires_in > current_time + 300:
                    return cached_data["access_token"]
            else:
                # 相对时间格式（秒数）
                if timestamp > 0 and current_time - timestamp < expires_in - 300:
                    return cached_data["access_token"]
    
    # 获取 OAuth 配置文件路径
    oauth_config_path = os.getenv("COZE_OAUTH_CONFIG_PATH", "coze_oauth_config.json")
    
    # 加载 OAuth 应用
    oauth_app = load_coze_oauth_app(oauth_config_path)
    
    # 在异步环境中运行同步的 get_access_token 方法
    try:
        oauth_token = await asyncio.to_thread(oauth_app.get_access_token)
        
        # 缓存 token
        expires_in = oauth_token.expires_in
        # 判断 expires_in 是时间戳还是相对时间
        if isinstance(expires_in, (int, float)) and expires_in > 1000000000:
            # 时间戳格式（绝对时间）
            _coze_token_cache = {
                "access_token": oauth_token.access_token,
                "expires_in": expires_in,
                "timestamp": current_time
            }
        else:
            # 相对时间格式（秒数），保存相对时间和时间戳
            _coze_token_cache = {
                "access_token": oauth_token.access_token,
                "expires_in": expires_in if isinstance(expires_in, (int, float)) else 3600,
                "timestamp": current_time
            }
        
        return oauth_token.access_token
        
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get Coze access token: {str(e)}"
        )


async def generate_image_via_coze(digest: str) -> str:
    """
    通过 Coze 工作流生成图片
    
    Args:
        digest: 文章摘要，作为工作流输入
        
    Returns:
        图片 URL 字符串
        
    Raises:
        HTTPException: 当工作流执行失败或找不到图片 URL 时
    """
    workflow_id = os.getenv("COZE_WORKFLOW_ID")
    if not workflow_id:
        raise HTTPException(
            status_code=500,
            detail="COZE_WORKFLOW_ID environment variable is not set"
        )
    
    workflow_param_name = os.getenv("COZE_WORKFLOW_PARAM_NAME", "content")
    coze_api_base = os.getenv("COZE_API_BASE", COZE_CN_BASE_URL)
    
    try:
        # 获取 Coze access token（使用 OAuth 方式）
        coze_api_token = await get_coze_access_token()
        
        # 初始化 Coze 客户端
        coze = AsyncCoze(
            auth=AsyncTokenAuth(coze_api_token),
            base_url=coze_api_base
        )
        
        # 执行工作流
        workflow_run = await coze.workflows.runs.create(
            workflow_id=workflow_id,
            parameters={workflow_param_name: digest}
        )
        
        # 从工作流结果中提取图片 URL
        # Coze 返回值格式：data = '{"image":"https://s.coze.cn/t/fdde0_ccsEk/"}'
        if not hasattr(workflow_run, "data") or not workflow_run.data:
            raise HTTPException(
                status_code=500,
                detail="Workflow result does not contain 'data' field"
            )
        
        try:
            # 解析 data 字段（可能是字符串或字典）
            raw_data = workflow_run.data
            if isinstance(raw_data, str):
                parsed_data = json.loads(raw_data)
            elif isinstance(raw_data, dict):
                parsed_data = raw_data
            else:
                raise ValueError(f"Unexpected data type: {type(raw_data)}")
            
            # 提取 image 字段
            if "image" not in parsed_data:
                raise HTTPException(
                    status_code=500,
                    detail="Workflow result does not contain 'image' field in data"
                )
            
            image_url = parsed_data["image"]
            if not isinstance(image_url, str) or not image_url.startswith(("http://", "https://")):
                raise HTTPException(
                    status_code=500,
                    detail=f"Invalid image URL format: {image_url}"
                )
        except json.JSONDecodeError as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to parse workflow data as JSON: {str(e)}"
            )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to extract image URL from workflow result: {str(e)}"
            )
        
        return image_url
        
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to generate image via Coze: {str(e)}"
        )


async def download_image(image_url: str) -> bytes:
    """
    下载图片
    
    Args:
        image_url: 图片 URL
        
    Returns:
        图片二进制数据
        
    Raises:
        HTTPException: 当下载失败时
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(image_url, timeout=30.0, follow_redirects=True)
            response.raise_for_status()
            
            # 检查内容类型
            content_type = response.headers.get("content-type", "")
            if not content_type.startswith("image/"):
                raise HTTPException(
                    status_code=400,
                    detail=f"URL does not point to an image (content-type: {content_type})"
                )
            
            # 检查文件大小（微信公众号限制：图片大小不超过 10MB）
            image_data = response.content
            max_size = 10 * 1024 * 1024
            if len(image_data) > max_size:
                # 图片超过 10MB 时进行压缩
                try:
                    image = Image.open(BytesIO(image_data))
                    # 统一转成 RGB，避免部分格式（如 PNG）导致保存 JPEG 出错
                    if image.mode in ("RGBA", "P"):
                        image = image.convert("RGB")

                    # 先尝试直接通过降低质量压缩到 10MB 以内
                    quality = 90
                    compressed_data = image_data
                    while quality >= 50:
                        buffer = BytesIO()
                        image.save(buffer, format="JPEG", quality=quality, optimize=True)
                        compressed_data = buffer.getvalue()
                        if len(compressed_data) <= max_size:
                            break
                        quality -= 10

                    # 如果仍然超过 10MB，再尝试按比例缩小分辨率一次
                    if len(compressed_data) > max_size:
                        width, height = image.size
                        # 按 0.7 比例缩放
                        new_size = (int(width * 0.7), int(height * 0.7))
                        image = image.resize(new_size)
                        buffer = BytesIO()
                        image.save(buffer, format="JPEG", quality=75, optimize=True)
                        compressed_data = buffer.getvalue()

                    if len(compressed_data) > max_size:
                        raise HTTPException(
                            status_code=400,
                            detail="Image size exceeds 10MB limit even after compression"
                        )

                    image_data = compressed_data
                except HTTPException:
                    raise
                except Exception as e:
                    raise HTTPException(
                        status_code=500,
                        detail=f"Failed to compress image: {str(e)}"
                    )

            return image_data
            
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to download image: {str(e)}"
        )


def prepare_image_for_wechat(image_data: bytes) -> tuple[bytes, str, str]:
    """
    将图片规范化为微信公众号更稳定接受的 JPEG。

    微信接口对图片大小和格式较敏感。自动化流程里图片来源可能是生成图、
    CDN 图或邮件图，这里统一压缩为 10MB 以内的 JPEG。
    """
    max_size = 10 * 1024 * 1024

    try:
        image = Image.open(BytesIO(image_data))
        if image.mode in ("RGBA", "P", "LA"):
            image = image.convert("RGB")

        quality = 90
        while quality >= 50:
            buffer = BytesIO()
            image.save(buffer, format="JPEG", quality=quality, optimize=True)
            prepared_data = buffer.getvalue()
            if len(prepared_data) <= max_size:
                return prepared_data, "image.jpg", "image/jpeg"
            quality -= 10

        width, height = image.size
        while len(prepared_data) > max_size and width > 800 and height > 800:
            width = int(width * 0.85)
            height = int(height * 0.85)
            image = image.resize((width, height))
            buffer = BytesIO()
            image.save(buffer, format="JPEG", quality=75, optimize=True)
            prepared_data = buffer.getvalue()

        if len(prepared_data) > max_size:
            raise HTTPException(
                status_code=400,
                detail="Image size exceeds 10MB limit even after compression"
            )

        return prepared_data, "image.jpg", "image/jpeg"
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid image data: {str(e)}"
        )


async def upload_image_to_wechat(image_data: bytes, access_token: str) -> str:
    """
    上传图片到微信公众号（永久素材）
    
    Args:
        image_data: 图片二进制数据
        access_token: 微信 access_token
        
    Returns:
        media_id 字符串
        
    Raises:
        HTTPException: 当上传失败时
    """
    url = "https://api.weixin.qq.com/cgi-bin/material/add_material"
    params = {
        "access_token": access_token,
        "type": "image"
    }
    
    try:
        image_data, filename, content_type = prepare_image_for_wechat(image_data)

        async with httpx.AsyncClient() as client:
            # 使用 multipart/form-data 上传文件
            files = {
                "media": (filename, image_data, content_type)
            }
            response = await client.post(url, params=params, files=files, timeout=30.0)
            response.raise_for_status()
            data = response.json()
            
            if "media_id" in data:
                return data["media_id"]
            else:
                error_msg = data.get("errmsg", "Unknown error")
                error_code = data.get("errcode", -1)
                raise HTTPException(
                    status_code=400,
                    detail=f"Failed to upload image to WeChat: {error_msg} (errcode: {error_code})"
                )
                
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Network error while uploading image: {str(e)}"
        )


async def upload_article_image_to_wechat(image_data: bytes, access_token: str) -> str:
    """
    上传正文内图片，返回可写入 content HTML 的微信图片 URL。
    """
    url = "https://api.weixin.qq.com/cgi-bin/media/uploadimg"
    params = {"access_token": access_token}

    try:
        image_data, filename, content_type = prepare_image_for_wechat(image_data)

        async with httpx.AsyncClient() as client:
            files = {
                "media": (filename, image_data, content_type)
            }
            response = await client.post(url, params=params, files=files, timeout=30.0)
            response.raise_for_status()
            data = response.json()

            if "url" in data:
                return data["url"]

            error_msg = data.get("errmsg", "Unknown error")
            error_code = data.get("errcode", -1)
            raise HTTPException(
                status_code=400,
                detail=f"Failed to upload article image to WeChat: {error_msg} (errcode: {error_code})"
            )
    except httpx.HTTPError as e:
        raise HTTPException(
            status_code=500,
            detail=f"Network error while uploading article image: {str(e)}"
        )


async def get_access_token(app_id: str, app_secret: str) -> str:
    """
    获取微信 access_token，带缓存机制
    
    Args:
        app_id: 微信公众号 AppID
        app_secret: 微信公众号 AppSecret
        
    Returns:
        access_token 字符串
        
    Raises:
        HTTPException: 当获取 token 失败时
    """
    cache_key = f"{app_id}_{app_secret}"
    current_time = time.time()
    
    # 检查缓存，token 有效期 7200 秒，提前 300 秒刷新
    if cache_key in _token_cache:
        cached_data = _token_cache[cache_key]
        if current_time - cached_data["timestamp"] < 6900:  # 提前刷新
            return cached_data["token"]
    
    # 请求新的 access_token
    url = "https://api.weixin.qq.com/cgi-bin/token"
    params = {
        "grant_type": "client_credential",
        "appid": app_id,
        "secret": app_secret
    }
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, params=params, timeout=10.0)
            response.raise_for_status()
            data = response.json()
            
            if "access_token" in data:
                # 缓存 token
                _token_cache[cache_key] = {
                    "token": data["access_token"],
                    "timestamp": current_time
                }
                return data["access_token"]
            else:
                error_msg = data.get("errmsg", "Unknown error")
                error_code = data.get("errcode", -1)
                raise HTTPException(
                    status_code=400,
                    detail=f"Failed to get access_token: {error_msg} (errcode: {error_code})"
                )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=500, detail=f"Network error: {str(e)}")


async def create_draft(request_data: Dict[str, Any], access_token: str) -> Dict[str, Any]:
    """
    创建微信草稿
    
    Args:
        request_data: 草稿数据
        access_token: 微信 access_token
        
    Returns:
        包含 media_id 的响应数据
        
    Raises:
        HTTPException: 当创建草稿失败时
    """
    url = "https://api.weixin.qq.com/cgi-bin/draft/add"
    params = {"access_token": access_token}
    
    # 构建请求体，articles 是数组
    payload = {
        "articles": [request_data]
    }
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, params=params, json=payload, timeout=30.0)
            response.raise_for_status()
            data = response.json()
            
            if "media_id" in data:
                return data
            else:
                error_msg = data.get("errmsg", "Unknown error")
                error_code = data.get("errcode", -1)
                raise HTTPException(
                    status_code=400,
                    detail=f"Failed to create draft: {error_msg} (errcode: {error_code})"
                )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=500, detail=f"Network error: {str(e)}")


async def publish_draft(media_id: str, access_token: str) -> Dict[str, Any]:
    """
    提交发布草稿。

    注意：个人公众号或未开通相关权限的账号，微信可能会返回权限错误。
    """
    url = "https://api.weixin.qq.com/cgi-bin/freepublish/submit"
    params = {"access_token": access_token}
    payload = {"media_id": media_id}

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(url, params=params, json=payload, timeout=30.0)
            response.raise_for_status()
            data = response.json()

            if data.get("errcode", 0) == 0 or "publish_id" in data:
                return data

            error_msg = data.get("errmsg", "Unknown error")
            error_code = data.get("errcode", -1)
            raise HTTPException(
                status_code=400,
                detail=f"Failed to publish draft: {error_msg} (errcode: {error_code})"
            )
    except httpx.HTTPError as e:
        raise HTTPException(status_code=500, detail=f"Network error: {str(e)}")


@app.post("/api/draft/create")
async def create_draft_endpoint(request: Union[NewsDraftRequest, NewspicDraftRequest]):
    """
    创建微信草稿接口
    
    根据 article_type 支持两种类型：
    - news: 图文消息，需要 thumb_media_id
    - newspic: 图片消息，需要 image_info
    """
    try:
        # 验证请求类型
        if isinstance(request, NewsDraftRequest):
            if request.article_type != "news":
                raise HTTPException(
                    status_code=400,
                    detail="For NewsDraftRequest, article_type must be 'news'"
                )
            
            # 获取 access_token（在生成封面图之前需要）
            app_id, app_secret = resolve_wechat_credentials(request.app_id, request.app_secret)
            access_token = await get_access_token(app_id, app_secret)
            
            if request.digest:
                try:
                    # 1. 调用 Coze 工作流生成图片 URL
                    image_url = await generate_image_via_coze(request.digest)

                    # 2. 下载图片
                    image_data = await download_image(image_url)
                    
                    # 3. 上传图片到公众号
                    media_id = await upload_image_to_wechat(image_data, access_token)
                    
                    # 4. 将返回的 media_id 赋值给 request.thumb_media_id
                    request.thumb_media_id = media_id
                except HTTPException as he:
                    logger.error(f"Failed to generate cover image: {str(he)}")
                except Exception as e:
                    logger.error(f"Failed to generate cover image: {str(e)}")
            
            # 验证 thumb_media_id 是否存在（如果既没有 digest 也没有 thumb_media_id，会在模型验证时失败）
            if not request.thumb_media_id:
                raise HTTPException(
                    status_code=400,
                    detail="thumb_media_id is required for news type (or provide digest to auto-generate)"
                )
        elif isinstance(request, NewspicDraftRequest):
            if request.article_type != "newspic":
                raise HTTPException(
                    status_code=400,
                    detail="For NewspicDraftRequest, article_type must be 'newspic'"
                )
            # 验证必填字段
            if not request.image_info or len(request.image_info.image_list) == 0:
                raise HTTPException(
                    status_code=400,
                    detail="image_info with at least one image is required for newspic type"
                )
            
            # 获取 access_token（对于 NewspicDraftRequest，在这里获取）
            app_id, app_secret = resolve_wechat_credentials(request.app_id, request.app_secret)
            access_token = await get_access_token(app_id, app_secret)
        
        # 构建请求数据，排除 app_id 和 app_secret
        request_dict = request.model_dump(exclude={"app_id", "app_secret"})
        
        # 创建草稿
        result = await create_draft(request_dict, access_token)
        
        return {
            "status": "success",
            "draft": result
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@app.post("/api/material/upload-image-url")
async def upload_image_url_endpoint(request: ImageUploadFromUrlRequest):
    """
    通过图片 URL 上传公众号素材。

    - usage=cover：上传永久图片素材，返回 media_id，可作为封面 thumb_media_id
    - usage=content：上传正文图片，返回 url，可写入文章 HTML 的 img src
    """
    app_id, app_secret = resolve_wechat_credentials(request.app_id, request.app_secret)
    access_token = await get_access_token(app_id, app_secret)
    image_data = await download_image(request.image_url)

    if request.usage == "cover":
        media_id = await upload_image_to_wechat(image_data, access_token)
        return {
            "status": "success",
            "usage": request.usage,
            "media_id": media_id
        }

    image_url = await upload_article_image_to_wechat(image_data, access_token)
    return {
        "status": "success",
        "usage": request.usage,
        "url": image_url
    }


@app.post("/api/material/upload-image-file")
async def upload_image_file_endpoint(
    app_id: Optional[str] = Form(None),
    app_secret: Optional[str] = Form(None),
    usage: str = Form("cover"),
    media: UploadFile = File(...)
):
    """
    通过 multipart 文件上传公众号素材。
    """
    if usage not in ["cover", "content"]:
        raise HTTPException(status_code=400, detail='usage must be "cover" or "content"')

    image_data = await media.read()
    resolved_app_id, resolved_app_secret = resolve_wechat_credentials(app_id, app_secret)
    access_token = await get_access_token(resolved_app_id, resolved_app_secret)

    if usage == "cover":
        media_id = await upload_image_to_wechat(image_data, access_token)
        return {
            "status": "success",
            "usage": usage,
            "media_id": media_id
        }

    image_url = await upload_article_image_to_wechat(image_data, access_token)
    return {
        "status": "success",
        "usage": usage,
        "url": image_url
    }


@app.post("/api/article/create-draft")
async def create_article_draft_endpoint(request: ArticleDraftRequest):
    """
    创建完整公众号图文草稿。

    自动化任务推荐调用这个接口：
    - 可以直接传 thumb_image_url，服务会上传封面并创建草稿
    - content 应传入已经排好版、图片 src 已替换为微信图片 URL 的 HTML
    - publish=true 时会在创建草稿后尝试提交发布
    """
    app_id, app_secret = resolve_wechat_credentials(request.app_id, request.app_secret)
    access_token = await get_access_token(app_id, app_secret)

    thumb_media_id = request.thumb_media_id
    if not thumb_media_id and request.thumb_image_url:
        image_data = await download_image(request.thumb_image_url)
        thumb_media_id = await upload_image_to_wechat(image_data, access_token)

    article = {
        "article_type": "news",
        "title": request.title,
        "content": request.content,
        "thumb_media_id": thumb_media_id,
        "author": request.author,
        "digest": request.digest,
        "content_source_url": request.content_source_url,
        "need_open_comment": request.need_open_comment,
        "only_fans_can_comment": request.only_fans_can_comment,
        "pic_crop_235_1": request.pic_crop_235_1,
        "pic_crop_1_1": request.pic_crop_1_1,
    }
    article = {key: value for key, value in article.items() if value is not None}

    draft_result = await create_draft(article, access_token)
    response: Dict[str, Any] = {
        "status": "success",
        "draft": draft_result
    }
    if thumb_media_id:
        response["thumb_media_id"] = thumb_media_id

    if request.publish:
        publish_result = await publish_draft(draft_result["media_id"], access_token)
        response["publish"] = publish_result

    return response


@app.post("/api/draft/publish")
async def publish_draft_endpoint(request: PublishDraftRequest):
    """
    提交发布已有草稿。
    """
    app_id, app_secret = resolve_wechat_credentials(request.app_id, request.app_secret)
    access_token = await get_access_token(app_id, app_secret)
    result = await publish_draft(request.media_id, access_token)
    return {
        "status": "success",
        "publish": result
    }


@app.get("/")
async def root():
    """根路径，返回 API 信息"""
    return {
        "name": "微信草稿 API",
        "version": "0.1.0",
        "endpoints": {
            "create_draft": "/api/draft/create (POST)",
            "upload_image_url": "/api/material/upload-image-url (POST)",
            "upload_image_file": "/api/material/upload-image-file (POST)",
            "create_article_draft": "/api/article/create-draft (POST)",
            "publish_draft": "/api/draft/publish (POST)",
            "html_draft_studio": "/ui (GET)"
        }
    }


@app.get("/ui", response_class=FileResponse)
async def html_draft_studio():
    """HTML 粘贴转微信公众号草稿工作台。"""
    return FileResponse(BASE_DIR / "html_draft_studio.html")


@app.get("/health")
async def health():
    """健康检查"""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
