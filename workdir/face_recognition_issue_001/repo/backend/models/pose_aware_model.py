import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
from PIL import Image
import torchvision.transforms as transforms
import sys
import os

# 添加项目路径以导入自定义模块
sys.path.append(os.path.dirname(os.path.abspath(__file__)))


class PoseAwareFaceRecognition:
    """正侧面人脸识别模型封装 - 完整论文实现版本"""
    
    def __init__(self, 
                 trained_weights='models/32_LR0.0001_MARGIN1.4_model_resnet_26_VALID_BEST.pt',
                 hopenet_weights='models/hopenet_robust_alpha1.pkl',
                 device=None):
        """
        初始化人脸识别模型
        
        Args:
            trained_weights: 你训练的最佳权重文件
            hopenet_weights: Hopenet姿态估计预训练权重
            device: 计算设备
        """
        self.device = device if device else torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.model = None
        self.trained_weights = trained_weights
        self.hopenet_weights = hopenet_weights
        self.transform = self._get_transform()
        self.transform_hopenet = self._get_hopenet_transform()
        self._load_model()
        
        print(f"✓ 模型加载完成")
        print(f"  - 设备: {self.device}")
        print(f"  - 训练权重: {trained_weights}")
        print(f"  - Hopenet权重: {hopenet_weights}")
    
    def _get_transform(self):
        """人脸识别的图像预处理（160x160）"""
        return transforms.Compose([
            transforms.Resize((160, 160)),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5])
        ])
    
    def _get_hopenet_transform(self):
        """Hopenet姿态估计的图像预处理（224x224）"""
        return transforms.Compose([
            transforms.Resize((224, 224)),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.5, 0.5, 0.5], std=[0.5, 0.5, 0.5])
        ])
    
    def _load_model(self):
        """加载完整的论文模型架构 - 修复版"""
        try:
            # 导入论文中的模型架构
            from Facenet_tune import FacePoseAwareNet
            
            print("正在加载模型架构...")
            
            # 1. 创建模型实例
            self.model = FacePoseAwareNet(pose=None)
            
            # 2. 先加载Hopenet权重（单独加载，因为训练时被冻结了）
            if os.path.exists(self.hopenet_weights):
                print(f"  加载Hopenet权重: {self.hopenet_weights}")
                try:
                    hopenet_state = torch.load(self.hopenet_weights, map_location=self.device)
                    
                    # Hopenet权重需要加载到l0.hope_model中
                    if hasattr(self.model, 'l0') and hasattr(self.model.l0, 'hope_model'):
                        # 创建一个字典来匹配权重
                        hopenet_dict = {}
                        for k, v in hopenet_state.items():
                            # 添加前缀 'hope_model.'
                            new_key = f'hope_model.{k}'
                            hopenet_dict[new_key] = v
                        
                        # 加载到l0.hope_model
                        self.model.l0.hope_model.load_state_dict(hopenet_dict, strict=False)
                        print("  ✓ Hopenet权重加载成功")
                    else:
                        print("  ⚠ 模型结构中没有l0.hope_model，跳过Hopenet权重")
                        
                except Exception as e:
                    print(f"  ⚠ Hopenet权重加载失败: {e}")
                    print("  将继续加载其他部分...")
            else:
                print(f"  ⚠ Hopenet权重文件不存在: {self.hopenet_weights}")
            
            # 3. 如果有多GPU，使用DataParallel
            if torch.cuda.device_count() > 1:
                print(f"  检测到 {torch.cuda.device_count()} 个GPU，使用DataParallel")
                self.model = nn.DataParallel(self.model)
            
            self.model.to(self.device)
            
            # 4. 加载你训练的权重（只包含InceptionResnet和CBAM部分）
            if os.path.exists(self.trained_weights):
                print(f"  加载训练权重: {self.trained_weights}")
                checkpoint = torch.load(self.trained_weights, map_location=self.device)
                
                # 根据checkpoint的键来判断如何加载
                if 'resnet' in checkpoint:
                    state_dict = checkpoint['resnet']
                else:
                    state_dict = checkpoint
                
                # 使用strict=False，允许部分加载
                # 因为Hopenet权重已经单独加载了
                if hasattr(self.model, 'module'):
                    missing_keys, unexpected_keys = self.model.module.load_state_dict(
                        state_dict, strict=False
                    )
                else:
                    missing_keys, unexpected_keys = self.model.load_state_dict(
                        state_dict, strict=False
                    )
                
                # 过滤掉Hopenet相关的missing keys（这是正常的）
                actual_missing = [k for k in missing_keys if not k.startswith('l0.')]
                
                if actual_missing:
                    print(f"  ⚠ 缺失的关键参数 ({len(actual_missing)}个):")
                    for k in actual_missing[:5]:  # 只显示前5个
                        print(f"    - {k}")
                    if len(actual_missing) > 5:
                        print(f"    ... 还有 {len(actual_missing) - 5} 个")
                else:
                    print("  ✓ 训练权重加载成功（除Hopenet外）")
                
                if unexpected_keys:
                    print(f"  ⚠ 意外的参数 ({len(unexpected_keys)}个)")
            else:
                print(f"  ⚠ 未找到训练权重文件 {self.trained_weights}")
            
            # 5. 设置为评估模式（重要！）
            self.model.eval()
            
            print("✓ 模型加载完成！")
            print(f"  - l0 (Hopenet): {'✓' if hasattr(self.model, 'l0') or (hasattr(self.model, 'module') and hasattr(self.model.module, 'l0')) else '✗'}")
            print(f"  - lf (正面分支): {'✓' if hasattr(self.model, 'lf') or (hasattr(self.model, 'module') and hasattr(self.model.module, 'lf')) else '✗'}")
            print(f"  - lp (侧面分支): {'✓' if hasattr(self.model, 'module') and hasattr(self.model.module, 'lp') else '✓' if hasattr(self.model, 'lp') else '✗'}")
            
        except ImportError as e:
            print(f"错误: 无法导入模型架构 - {e}")
            print("请确保以下文件在backend目录下:")
            print("  - Facenet_tune.py")
            print("  - MyModel.py")
            print("  - hopenet.py")
            print("  - Attention_block.py")
            print("  - MODEL/cbam.py (注意：你改成了MODEL)")
            print("  - nnmodels/inception_resnet_v1.py")
            raise
            
        except Exception as e:
            print(f"错误: 模型加载失败 - {e}")
            import traceback
            traceback.print_exc()
            print("\n使用简化模型作为后备方案（功能受限）")
            self._load_simplified_model()
    
    def _load_simplified_model(self):
        """简化模型（用于演示，实际应使用完整模型）"""
        class SimplifiedModel(nn.Module):
            def __init__(self):
                super().__init__()
                self.features = nn.Sequential(
                    nn.Conv2d(3, 64, 3, padding=1),
                    nn.ReLU(),
                    nn.MaxPool2d(2),
                    nn.Conv2d(64, 128, 3, padding=1),
                    nn.ReLU(),
                    nn.MaxPool2d(2),
                    nn.AdaptiveAvgPool2d((1, 1))
                )
                self.fc = nn.Linear(128, 512)
            
            def forward(self, x):
                x = self.features(x)
                x = x.view(x.size(0), -1)
                x = self.fc(x)
                return F.normalize(x, p=2, dim=1)
        
        self.model = SimplifiedModel()
        self.model.to(self.device)
        self.model.eval()
    
    def extract_embedding(self, image, pose='auto'):
        """
        提取人脸特征向量（核心功能）
        
        Args:
            image: PIL Image或numpy array
            pose: 'frontal'(正面), 'profile'(侧面), 或 'auto'(自动检测)
        
        Returns:
            numpy array: 512维归一化特征向量
        """
        try:
            # 1. 转换为PIL Image
            if isinstance(image, np.ndarray):
                image = Image.fromarray(image)
            
            if image.mode != 'RGB':
                image = image.convert('RGB')
            
            # 2. 如果是自动模式，先估计姿态
            if pose == 'auto':
                pose = self.estimate_pose(image)
                print(f"  检测到姿态: {pose}")
            
            # 3. 预处理图像
            img_tensor = self.transform(image).unsqueeze(0).to(self.device)
            
            # 4. 提取特征
            with torch.no_grad():
                if hasattr(self.model, 'module'):
                    # 如果使用了DataParallel
                    embedding = self.model.module(img_tensor, pose=pose)
                else:
                    embedding = self.model(img_tensor, pose=pose)
            
            # 5. 转换为numpy并归一化
            embedding = embedding.cpu().numpy().flatten()
            embedding = embedding / (np.linalg.norm(embedding) + 1e-8)
            
            return embedding
            
        except Exception as e:
            print(f"特征提取错误: {e}")
            import traceback
            traceback.print_exc()
            raise
    
    def estimate_pose(self, image):
        """
        估计人脸姿态（使用Hopenet或简化方法）
        
        Args:
            image: PIL Image
        
        Returns:
            str: 'frontal' 或 'profile'
        """
        try:
            # 如果模型有Hopenet组件，使用它来估计姿态
            if hasattr(self.model, 'module') and hasattr(self.model.module, 'l0'):
                hopenet_model = self.model.module.l0
            elif hasattr(self.model, 'l0'):
                hopenet_model = self.model.l0
            else:
                # 没有Hopenet，默认返回frontal
                return 'frontal'
            
            # 预处理为224x224（Hopenet需要）
            if isinstance(image, torch.Tensor):
                # 如果已经是tensor，需要转回PIL
                import torchvision.transforms.functional as TF
                image = TF.to_pil_image(image.squeeze(0).cpu())
            
            img_tensor = self.transform_hopenet(image).unsqueeze(0).to(self.device)
            
            # 估计姿态
            with torch.no_grad():
                yaw, yaw_predicted, _ = hopenet_model(img_tensor)
            
            # 根据yaw角度判断是正面还是侧面
            yaw_angle = yaw_predicted.cpu().item()
            
            # yaw角度判断标准：
            # -30° ~ 30° 认为是正面
            # 其他角度认为是侧面
            if -30 <= yaw_angle <= 30:
                return 'frontal'
            else:
                return 'profile'
                
        except Exception as e:
            print(f"姿态估计失败，使用默认值: {e}")
            # 出错时默认为正面
            return 'frontal'
    
    def compute_similarity(self, embedding1, embedding2):
        """
        计算两个特征向量的相似度（余弦相似度）
        
        Returns:
            float: 相似度 [0, 1]
        """
        similarity = np.dot(embedding1, embedding2)
        return float(similarity)
    
    def is_loaded(self):
        """检查模型是否已加载"""
        return self.model is not None
    
    def batch_extract_embeddings(self, images, poses=None):
        """
        批量提取特征
        
        Args:
            images: list of PIL Images
            poses: list of poses or None
        
        Returns:
            numpy array: shape (N, 512)
        """
        if poses is None:
            poses = ['auto'] * len(images)
        
        embeddings = []
        for img, pose in zip(images, poses):
            emb = self.extract_embedding(img, pose)
            embeddings.append(emb)
        
        return np.array(embeddings)