import base64
import re
from pathlib import Path
from typing import Optional
import aiofiles
import magic
from httpx import AsyncClient, Timeout
from pydantic import Field
from nekro_agent.api.core import logger
from nekro_agent.api.schemas import AgentCtx
from nekro_agent.core.config import config as global_config
from nekro_agent.services.agent.creator import ContentSegment, OpenAIChatMessage
from nekro_agent.services.plugin.base import ConfigBase, NekroPlugin, SandboxMethodType
from nekro_agent.tools.path_convertor import convert_to_host_path

plugin = NekroPlugin(
    name="gemini绘画插件",
    module_name="nekro_plugin_draw_gemini",
    description="gemini绘画插件",
    version="0.1.0",
    author="Thanatos",
    url="https://github.com/ThanatosIng/nekro-plugin-draw-gemini",
)


@plugin.mount_config()
class DrawConfig(ConfigBase):
    """绘画配置"""

    USE_DRAW_MODEL_GROUP: str = Field(
        default="default-draw-chat",
        title="绘图模型组",
        json_schema_extra={"ref_model_groups": True, "required": True},
        description="主要使用的绘图模型组，可在 `系统配置` -> `模型组` 选项卡配置",
    )
    NUM_INFERENCE_STEPS: int = Field(default=20, title="模型推理步数")


# 获取配置
config: DrawConfig = plugin.get_config(DrawConfig)


@plugin.mount_sandbox_method(SandboxMethodType.TOOL, name="gemini绘图", description="支持文生图和图生图")
async def gemini_draw(
    _ctx: AgentCtx,
    prompt: str,
    size: str = "1024x1024",
    refer_image: str = "",
) -> str:
    """Generate or modify images

    Args:
        prompt (str): Natural language description of the image you want to create. (Only supports English)
            Suggested elements to include:
            - Type of drawing (e.g., character setting, landscape, comics, etc.)
            - What to draw details (characters, animals, objects, etc.)
            - What they are doing or their state
            - The scene or environment
            - Overall mood or atmosphere
            - Very detailed description or story (optional, recommend for comics)
            - Art style (e.g., illustration, watercolor... any style you want)

        size (str): Image dimensions (e.g., "1024x1024" square, "512x768" portrait, "768x512" landscape)
        guidance_scale (float): Guidance scale for the image generation, lower is more random, higher is more like the prompt (default: 7.5, from 0 to 20)
        refer_image (str): Optional source image path for image reference (useful for image style transfer or keep the elements of the original image)

    Returns:
        str: Generated image URL

    Examples:
        # Generate new image
        send_msg_file(chat_key, gemini_draw("a illustration style cute orange cat napping on a sunny windowsill, watercolor painting style", "1024x1024"))

        # Modify existing image
        send_msg_file(chat_key, gemini_draw("change the background to a cherry blossom park, keep the anime style", "1024x1024", "shared/refer_image.jpg"))
    """
    global last_successful_mode
    # logger.info(f"绘图提示: {prompt}")
    # logger.info(f"绘图尺寸: {size}")
    logger.info(f"使用绘图模型组: {config.USE_DRAW_MODEL_GROUP} 绘制: {prompt}")
    if refer_image:
        async with aiofiles.open(
            convert_to_host_path(Path(refer_image), chat_key=_ctx.from_chat_key, container_key=_ctx.container_key),
            mode="rb",
        ) as f:
            image_data = await f.read()
            mime_type = magic.from_buffer(image_data, mime=True)
            image_data = base64.b64encode(image_data).decode("utf-8")
        source_image_data = f"data:{mime_type};base64,{image_data}"
    else:
        source_image_data = "data:image/webp;base64, XXX"
    if config.USE_DRAW_MODEL_GROUP not in global_config.MODEL_GROUPS:
        raise Exception(f"绘图模型组 `{config.USE_DRAW_MODEL_GROUP}` 未配置")
    model_group = global_config.MODEL_GROUPS[config.USE_DRAW_MODEL_GROUP]

    return await _chat_image(model_group, prompt, size, refer_image, source_image_data)


async def _chat_image(model_group, prompt, size, refer_image, source_image_data):
    """使用聊天模式绘图"""
    msg = OpenAIChatMessage.create_empty("user")
    if refer_image:
        msg = msg.add(ContentSegment.image_content(source_image_data))
        msg = msg.add(
            ContentSegment.text_content(
                f"You are a professional painter. Use your high-quality drawing skills to draw a picture based on the user's description. Just provide the image and do not ask for more information. Carefully analyze the above image and make a picture based on the following description: {prompt} (size: {size})",
            ),
        )
    else:
        msg = msg.add(
            ContentSegment.text_content(
                f"You are a professional painter. Use your high-quality drawing skills to draw a picture based on the user's description. Just provide the image and do not ask for more information. Make a picture based on the following description: {prompt} (size: {size})"
            ),
        )
    async with AsyncClient() as client:
        response = await client.post(
            f"{model_group.BASE_URL}/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {model_group.API_KEY}",
            },
            json={
                "model": model_group.CHAT_MODEL,
                "messages": [
                    msg.to_dict(),
                ],
            },
            timeout=Timeout(read=60, write=60, connect=10, pool=10),
        )
        response.raise_for_status()
        data = response.json()
    # logger.info(f"绘图响应: {data}")
    content = data["choices"][0]["message"]["content"]
    if content:
        logger.info(f"绘图地址: {content}")
        pattern = r"!\[.*?\]\((.*?)\)"  # 匹配 Markdown 图片语法
        ret_file_url = re.search(pattern, content)
        if ret_file_url:
            ret_file_url = ret_file_url.group(1)
        else:
            logger.error(f"绘图响应中未找到图片信息: {data}")
            raise Exception(
                "No image found in image generation AI response. You can adjust the prompt and try again. Make sure the prompt is clear and detailed.",
            )
    else:
        logger.error(f"绘图响应中未找到图片信息: {data}")
        raise Exception(
            "No image found in image generation AI response. You can adjust the prompt and try again. Make sure the prompt is clear and detailed.",
        )
    return ret_file_url


@plugin.mount_cleanup_method()
async def clean_up():
    """清理插件"""
    logger.info("gemini绘画插件插件已清理完毕")
