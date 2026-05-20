import os
import sys
import requests
from pathlib import Path

# 百度搜索资源平台 API 配置
# 请将 YOUR_BAIDU_TOKEN 替换为您在百度搜索资源平台获取的真实 Token
BAIDU_TOKEN = os.environ.get("BAIDU_TOKEN", "YOUR_BAIDU_TOKEN")
SITE_URL = "https://www.fixepub.com"
API_URL = f"http://data.zz.baidu.com/urls?site={SITE_URL}&token={BAIDU_TOKEN}"

def get_urls_from_sitemap():
    """从 sitemap.xml 中提取所有的 URL"""
    sitemap_path = Path(__file__).resolve().parent.parent / "frontend" / "sitemap.xml"
    urls = []
    
    if not sitemap_path.exists():
        print(f"Error: 找不到 sitemap.xml 文件: {sitemap_path}")
        return urls
        
    try:
        import xml.etree.ElementTree as ET
        tree = ET.parse(sitemap_path)
        root = tree.getroot()
        # 处理带有 namespace 的 XML
        namespace = {'ns': 'http://www.sitemaps.org/schemas/sitemap/0.9'}
        for url_elem in root.findall('ns:url', namespace):
            loc = url_elem.find('ns:loc', namespace)
            if loc is not None and loc.text:
                urls.append(loc.text.strip())
    except Exception as e:
        print(f"Error 解析 sitemap.xml: {e}")
        
    return urls

def push_to_baidu(urls):
    """将 URL 列表主动推送给百度"""
    if not urls:
        print("没有找到需要推送的 URL。")
        return
        
    if BAIDU_TOKEN == "YOUR_BAIDU_TOKEN":
        print("⚠️ 警告: 请先在脚本中或通过环境变量设置真实的 BAIDU_TOKEN")
        print("可以在百度搜索资源平台 -> 资源提交 -> 普通收录 -> 资源提交 -> API提交 中找到您的 token")
        return
        
    headers = {
        'Content-Type': 'text/plain'
    }
    
    # 百度 API 要求每行一个 URL
    data = '\n'.join(urls)
    
    print(f"正在向百度推送 {len(urls)} 个 URL...")
    for u in urls:
        print(f"  - {u}")
        
    try:
        response = requests.post(API_URL, headers=headers, data=data, timeout=10)
        result = response.json()
        
        if response.status_code == 200:
            print("\n✅ 推送成功!")
            print(f"成功推送数量: {result.get('success', 0)}")
            print(f"今日剩余额度: {result.get('remain', 0)}")
            if 'not_same_site' in result:
                print(f"非本站 URL: {result['not_same_site']}")
            if 'not_valid' in result:
                print(f"不合法 URL: {result['not_valid']}")
        else:
            print(f"\n❌ 推送失败 (HTTP {response.status_code}):")
            print(f"错误码: {result.get('error', '未知')}")
            print(f"错误信息: {result.get('message', '未知')}")
            
    except Exception as e:
        print(f"\n❌ 请求发生异常: {e}")

if __name__ == "__main__":
    urls_to_push = get_urls_from_sitemap()
    push_to_baidu(urls_to_push)
