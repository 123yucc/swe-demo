"""
修复导入问题 - 自动检测和修复文件中的导入路径
"""
import os
import re


def fix_model_imports():
    """修复MODELS文件夹名称导致的导入问题"""
    
    print("="*60)
    print("修复导入路径")
    print("="*60)
    
    # 需要修复的文件列表
    files_to_fix = [
        'Attention_block.py',
        'Facenet_tune.py',
    ]
    
    fixed_count = 0
    
    for filename in files_to_fix:
        filepath = os.path.join(os.path.dirname(__file__), filename)
        
        if not os.path.exists(filepath):
            print(f"⚠ 文件不存在: {filename}")
            continue
        
        print(f"\n检查文件: {filename}")
        
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        original_content = content
        
        # 修复 from MODELS.xxx import 为 from MODEL.xxx import
        content = re.sub(r'from MODELS\.', 'from MODEL.', content)
        
        # 修复 import MODELS. 为 import MODEL.
        content = re.sub(r'import MODELS\.', 'import MODEL.', content)
        
        if content != original_content:
            # 创建备份
            backup_path = filepath + '.backup'
            with open(backup_path, 'w', encoding='utf-8') as f:
                f.write(original_content)
            print(f"  ✓ 创建备份: {backup_path}")
            
            # 写入修复后的内容
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(content)
            
            print(f"  ✓ 修复完成: MODELS → MODEL")
            fixed_count += 1
        else:
            print(f"  ✓ 无需修复")
    
    print("\n" + "="*60)
    print(f"修复完成！共修复 {fixed_count} 个文件")
    print("="*60)


def check_hopenet_weights():
    """检查Hopenet权重文件"""
    print("\n" + "="*60)
    print("检查Hopenet权重")
    print("="*60)
    
    hopenet_path = 'models/hopenet_robust_alpha1.pkl'
    
    if os.path.exists(hopenet_path):
        import torch
        try:
            state = torch.load(hopenet_path, map_location='cpu')
            print(f"✓ Hopenet权重文件存在")
            print(f"  参数数量: {len(state)} 个")
            print(f"  前5个键:")
            for i, k in enumerate(list(state.keys())[:5]):
                print(f"    {i+1}. {k}")
        except Exception as e:
            print(f"✗ 权重文件损坏: {e}")
    else:
        print(f"✗ Hopenet权重文件不存在: {hopenet_path}")
        print("  请确保文件路径正确")


def check_trained_weights():
    """检查训练权重文件"""
    print("\n" + "="*60)
    print("检查训练权重")
    print("="*60)
    
    trained_path = 'models/32_LR0.0001_MARGIN1.4_model_resnet_26_VALID_BEST.pt'
    
    if os.path.exists(trained_path):
        import torch
        try:
            checkpoint = torch.load(trained_path, map_location='cpu')
            print(f"✓ 训练权重文件存在")
            
            if isinstance(checkpoint, dict):
                print(f"  Checkpoint类型: dict")
                print(f"  顶层键: {list(checkpoint.keys())}")
                
                if 'resnet' in checkpoint:
                    state_dict = checkpoint['resnet']
                    print(f"  模型参数数量: {len(state_dict)} 个")
                    
                    # 检查包含哪些组件
                    has_hopenet = any(k.startswith('l0.') for k in state_dict.keys())
                    has_attention = any(k.startswith('l_a.') for k in state_dict.keys())
                    has_frontal = any(k.startswith('lf.') for k in state_dict.keys())
                    has_profile = any(k.startswith('lp.') for k in state_dict.keys())
                    
                    print(f"\n  包含的组件:")
                    print(f"    - Hopenet (l0): {'✓' if has_hopenet else '✗'}")
                    print(f"    - Attention (l_a): {'✓' if has_attention else '✗'}")
                    print(f"    - Frontal (lf): {'✓' if has_frontal else '✗'}")
                    print(f"    - Profile (lp): {'✓' if has_profile else '✗'}")
                    
                    if not has_hopenet:
                        print("\n  ⚠ 重要提示:")
                        print("    训练权重中不包含Hopenet参数（这是正常的）")
                        print("    Hopenet在训练时被冻结，需要单独加载")
            else:
                print(f"  Checkpoint类型: {type(checkpoint)}")
                
        except Exception as e:
            print(f"✗ 权重文件损坏: {e}")
    else:
        print(f"✗ 训练权重文件不存在: {trained_path}")
        print("  请确保文件路径正确")


def download_mtcnn_weights():
    """下载MTCNN权重"""
    print("\n" + "="*60)
    print("下载MTCNN权重")
    print("="*60)
    
    import urllib.request
    
    data_dir = 'nnmodels/data'
    os.makedirs(data_dir, exist_ok=True)
    
    mtcnn_files = {
        'pnet.pt': 'https://github.com/timesler/facenet-pytorch/releases/download/v2.2.9/pnet.pt',
        'rnet.pt': 'https://github.com/timesler/facenet-pytorch/releases/download/v2.2.9/rnet.pt',
        'onet.pt': 'https://github.com/timesler/facenet-pytorch/releases/download/v2.2.9/onet.pt'
    }
    
    for filename, url in mtcnn_files.items():
        filepath = os.path.join(data_dir, filename)
        
        if os.path.exists(filepath):
            print(f"✓ {filename} 已存在")
        else:
            print(f"下载 {filename}...")
            try:
                urllib.request.urlretrieve(url, filepath)
                print(f"✓ {filename} 下载成功")
            except Exception as e:
                print(f"✗ {filename} 下载失败: {e}")
                print(f"  请手动下载: {url}")


def main():
    """运行所有修复"""
    print("\n" + "🔧 " + "="*58)
    print("    自动修复工具")
    print("="*60)
    
    # 1. 修复导入路径
    fix_model_imports()
    
    # 2. 检查Hopenet权重
    check_hopenet_weights()
    
    # 3. 检查训练权重
    check_trained_weights()
    
    # 4. 下载MTCNN权重
    download_mtcnn_weights()
    
    print("\n" + "="*60)
    print("修复完成！现在可以运行: python app.py")
    print("="*60 + "\n")


if __name__ == '__main__':
    main()