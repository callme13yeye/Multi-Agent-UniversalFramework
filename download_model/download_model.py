# ===============modelscope模型下载=====================
from modelscope import snapshot_download
import os
import shutil
model_class = 'BAAI/bge-reranker-v2-m3' # 'Qwen/Qwen3-Reranker-0.6B' # 'BAAI/bge-reranker-v2-m3' 'Qwen/Qwen3-Embedding-0.6B'
model_name = 'bge-reranker-v2-m3'
base_dir = r'D:\agentrag\models'
model_dir = os.path.join(base_dir, model_name)

os.makedirs(base_dir, exist_ok=True)
try:
    snapshot_download(model_class, local_dir=model_dir)
    print(f'{model_name}模型下载完成，已经存放到{model_dir}')
except Exception as e:
    print(f'下载过程中发生错误: {e}')
finally:
    temp_dir = os.path.join(model_dir, '._____temp')
    if os.path.exists(temp_dir):
        shutil.rmtree(temp_dir, ignore_errors=True)
        print(f'清理临时文件完成，请重新运行脚本进行下载')

