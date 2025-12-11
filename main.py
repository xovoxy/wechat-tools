"""
微信草稿 API Server
提供接口用于向微信公众号创建草稿
"""
import time
from typing import Optional, Dict, Any, Union
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
import httpx

app = FastAPI(title="微信草稿 API", description="微信公众号草稿创建服务")

# Access token 缓存
_token_cache: Dict[str, Dict[str, Any]] = {}


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
    title: str = Field(..., min_length=1, description="标题")
    content: str = Field(..., min_length=1, description="正文内容，支持HTML标签，小于20000字符且小于1MB")
    thumb_media_id: str = Field(..., description="封面图片 media_id，必须是永久 MediaID")
    app_id: str = Field(..., description="AppID")
    app_secret: str = Field(..., description="AppSecret")
    author: Optional[str] = Field(None, max_length=16, description="作者，最多16字符")
    digest: Optional[str] = Field(None, description="摘要，单图文才有，多图文为空")
    content_source_url: Optional[str] = Field(None, description="原文链接")
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


class NewspicDraftRequest(BaseModel):
    """图片消息草稿请求"""
    article_type: str = Field(default="newspic", description="文章类型")
    title: str = Field(..., min_length=1, description="标题")
    content: str = Field(..., min_length=1, description="正文内容，支持HTML标签，小于20000字符且小于1MB")
    image_info: ImageInfo = Field(..., description="图片信息，最多20张图片，第一张为封面")
    app_id: str = Field(..., description="AppID")
    app_secret: str = Field(..., description="AppSecret")
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

    @field_validator("image_info")
    @classmethod
    def validate_image_info(cls, v):
        if len(v.image_list) == 0:
            raise ValueError("image_list cannot be empty")
        if len(v.image_list) > 20:
            raise ValueError("image_list cannot exceed 20 images")
        return v


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
            # 验证必填字段
            if not request.thumb_media_id:
                raise HTTPException(
                    status_code=400,
                    detail="thumb_media_id is required for news type"
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
        
        # 获取 access_token
        access_token = await get_access_token(request.app_id, request.app_secret)
        
        # 构建请求数据，排除 app_id 和 app_secret
        request_dict = request.model_dump(exclude={"app_id", "app_secret"})
        
        # 创建草稿
        result = await create_draft(request_dict, access_token)
        
        return {
            "media_id": result["media_id"],
            "status": "success"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@app.get("/")
async def root():
    """根路径，返回 API 信息"""
    return {
        "name": "微信草稿 API",
        "version": "0.1.0",
        "endpoints": {
            "create_draft": "/api/draft/create (POST)"
        }
    }


@app.get("/health")
async def health():
    """健康检查"""
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
