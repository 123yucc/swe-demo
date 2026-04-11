"""
人脸识别系统配置文件
"""
import os

# ==================== 路径配置 ====================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 模型权重路径
TRAINED_WEIGHTS = os.path.join(BASE_DIR, 'models', '32_LR0.0001_MARGIN1.4_model_resnet_26_VALID_BEST.pt')
HOPENET_WEIGHTS = os.path.join(BASE_DIR, 'models', 'hopenet_robust_alpha1.pkl')

# 数据路径
DATABASE_PATH = os.path.join(BASE_DIR, 'data', 'face_database')
UPLOAD_PATH = os.path.join(BASE_DIR, 'data', 'uploads')

# ==================== 模型配置 ====================

# 人脸识别配置
FACE_RECOGNITION_CONFIG = {
    'image_size': 160,          # 输入图像尺寸
    'embedding_dim': 512,       # 特征向量维度
    'threshold': 0.6,           # 默认相似度阈值
    'use_gpu': True,            # 是否使用GPU
}

# 姿态估计配置
POSE_ESTIMATION_CONFIG = {
    'hopenet_image_size': 224,  # Hopenet输入尺寸
    'num_bins': 66,             # 姿态角度bins数量
    'frontal_range': (-30, 30), # 正面yaw角度范围
}

# MTCNN人脸检测配置
MTCNN_CONFIG = {
    'image_size': 160,
    'margin': 20,
    'min_face_size': 40,
    'thresholds': [0.6, 0.7, 0.7],
    'factor': 0.709,
    'post_process': False,
    'keep_all': True,
}

# ==================== 训练配置（如需继续训练）====================

TRAINING_CONFIG = {
    'batch_size': 32,
    'learning_rate': 0.0001,
    'margin': 1.4,              # Contrastive loss margin
    'epochs': 100,
    'patience': 15,             # Early stopping patience
    'gamma': 0.1,               # Learning rate decay factor
}

# ==================== 数据增强配置 ====================

AUGMENTATION_CONFIG = {
    'random_flip': False,       # 人脸识别通常不翻转
    'random_rotation': 0,       # 旋转角度范围
    'color_jitter': False,
}

# ==================== API配置 ====================

API_CONFIG = {
    'host': '0.0.0.0',
    'port': 5000,
    'debug': True,
    'max_content_length': 32 * 1024 * 1024,  # 32MB
    'cors_origins': '*',
}

# ==================== 数据库配置 ====================

DATABASE_CONFIG = {
    'max_faces': 10000,         # 最大存储人脸数
    'backup_interval': 3600,    # 备份间隔（秒）
    'compression': True,        # 是否压缩存储
}

# ==================== 日志配置 ====================

LOGGING_CONFIG = {
    'level': 'INFO',
    'format': '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    'file': os.path.join(BASE_DIR, 'logs', 'app.log'),
}

# ==================== 验证配置是否有效 ====================

def validate_config():
    """验证配置文件和必要文件是否存在"""
    errors = []
    
    # 检查模型权重文件
    if not os.path.exists(TRAINED_WEIGHTS):
        errors.append(f"训练权重文件不存在: {TRAINED_WEIGHTS}")
    
    if not os.path.exists(HOPENET_WEIGHTS):
        errors.append(f"Hopenet权重文件不存在: {HOPENET_WEIGHTS}")
    
    # 检查必要目录
    for path in [DATABASE_PATH, UPLOAD_PATH]:
        if not os.path.exists(path):
            try:
                os.makedirs(path, exist_ok=True)
                print(f"✓ 创建目录: {path}")
            except Exception as e:
                errors.append(f"无法创建目录 {path}: {e}")
    
    # 检查必要的Python文件
    required_files = [
        'Facenet_tune.py',
        'MyModel.py',
        'hopenet.py',
        'Attention_block.py',
    ]
    
    for file in required_files:
        file_path = os.path.join(BASE_DIR, file)
        if not os.path.exists(file_path):
            errors.append(f"缺少必要文件: {file}")
    
    # 检查必要目录
    required_dirs = [
        'MODEL',
        'nnmodels',
    ]
    
    for dir_name in required_dirs:
        dir_path = os.path.join(BASE_DIR, dir_name)
        if not os.path.exists(dir_path):
            errors.append(f"缺少必要目录: {dir_name}")
    
    if errors:
        print("\n❌ 配置验证失败:")
        for error in errors:
            print(f"  - {error}")
        return False
    else:
        print("\n✓ 配置验证通过!")
        return True


if __name__ == '__main__':
    validate_config()