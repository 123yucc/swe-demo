import numpy as np
from PIL import Image
import torch
import os
import urllib.request


class FaceDetector:
    """人脸检测器（使用MTCNN）"""
    
    def __init__(self, device=None):
        self.device = device if device else torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.mtcnn = None
        self._min_face_size_factor = 1.0  # Default factor for min_face_size
        self._confidence_threshold_offset = 0.0  # Default offset for confidence threshold
        self._ensure_mtcnn_data()
        self._load_detector()

    def set_detection_sensitivity(self, min_face_size_factor: float = 1.0, confidence_threshold_offset: float = 0.0):
        """
        配置人脸检测器的灵敏度。
        min_face_size_factor: 调整最小人脸尺寸的因子 (例如, 0.5 会将最小尺寸减半)。
        confidence_threshold_offset: 调整置信度阈值的偏移量 (例如, -0.1 会将阈值降低 0.1)。
        """
        if not (0.1 <= min_face_size_factor <= 2.0):
            print("Warning: min_face_size_factor should be between 0.1 and 2.0.")
        if not (-0.5 <= confidence_threshold_offset <= 0.5):
            print("Warning: confidence_threshold_offset should be between -0.5 and 0.5.")

        self._min_face_size_factor = min_face_size_factor
        self._confidence_threshold_offset = confidence_threshold_offset

    def _adjust_detection_parameters(self, image_width: int, image_height: int):
        """
        根据图像分辨率和配置的灵敏度因子动态调整检测参数。
        """
        base_min_face_size = 40  # 原始的最小人脸尺寸
        base_confidence_threshold = 0.9  # 原始的置信度阈值

        # 根据因子调整最小人脸尺寸
        adjusted_min_face_size = int(base_min_face_size * self._min_face_size_factor)
        # 确保最小人脸尺寸不小于某个合理值 (例如 20)
        adjusted_min_face_size = max(20, adjusted_min_face_size)

        # 根据偏移量调整置信度阈值
        adjusted_confidence_threshold = base_confidence_threshold + self._confidence_threshold_offset
        # 确保置信度阈值在合理范围内 (例如 0.5 到 0.99)
        adjusted_confidence_threshold = max(0.5, min(0.99, adjusted_confidence_threshold))

        # 可以根据图像分辨率进一步调整，例如，对于非常低分辨率的图像，进一步降低 min_face_size
        # 假设如果图像的任何维度小于200像素，就稍微降低min_face_size
        if image_width < 200 or image_height < 200:
            adjusted_min_face_size = max(15, int(adjusted_min_face_size * 0.8))

        return adjusted_min_face_size, adjusted_confidence_threshold

    def _ensure_mtcnn_data(self):
        """确保MTCNN预训练权重文件存在"""
        data_dir = os.path.join(os.path.dirname(__file__), '..', 'nnmodels', 'data')
        os.makedirs(data_dir, exist_ok=True)
        
        # MTCNN预训练权重文件
        mtcnn_files = {
            'pnet.pt': 'https://github.com/timesler/facenet-pytorch/releases/download/v2.2.9/pnet.pt',
            'rnet.pt': 'https://github.com/timesler/facenet-pytorch/releases/download/v2.2.9/rnet.pt',
            'onet.pt': 'https://github.com/timesler/facenet-pytorch/releases/download/v2.2.9/onet.pt'
        }
        
        for filename, url in mtcnn_files.items():
            filepath = os.path.join(data_dir, filename)
            if not os.path.exists(filepath):
                print(f"  下载MTCNN权重: {filename}...")
                try:
                    urllib.request.urlretrieve(url, filepath)
                    print(f"  ✓ {filename} 下载成功")
                except Exception as e:
                    print(f"  ✗ {filename} 下载失败: {e}")
                    print(f"  请手动下载 {url} 到 {filepath}")
    
    def _load_detector(self):
        """加载MTCNN检测器"""
        try:
            # 使用项目中的MTCNN
            from nnmodels.mtcnn import MTCNN
            
            self.mtcnn = MTCNN(
                image_size=160,
                margin=20,
                min_face_size=40,
                thresholds=[0.6, 0.7, 0.7],
                factor=0.709,
                post_process=False,
                device=self.device,
                keep_all=True
            )
            print("✓ MTCNN加载成功")
            
        except Exception as e:
            print(f"⚠ MTCNN加载失败: {e}")
            print("  将使用简化检测器（准确度较低）")
            self.mtcnn = None
    
    def detect_faces(self, image):
        """
        检测图像中的所有人脸
        
        Args:
            image: PIL Image或numpy array
        
        Returns:
            list of boxes: [[x1, y1, x2, y2], ...]
        """
        try:
            # 转换为PIL Image
            if isinstance(image, np.ndarray):
                image = Image.fromarray(image)
            
            if self.mtcnn is not None:
                # 使用MTCNN检测
                boxes, probs = self.mtcnn.detect(image)
                
                if boxes is not None:
                    # 过滤低置信度检测
                    valid_boxes = []
                    for box, prob in zip(boxes, probs):
                        if prob > 0.9:  # 置信度阈值
                            valid_boxes.append(box)
                    
                    return np.array(valid_boxes) if valid_boxes else np.array([])
                else:
                    return np.array([])
            else:
                # 简化检测器（整图作为人脸）
                w, h = image.size
                return np.array([[0, 0, w, h]])
                
        except Exception as e:
            print(f"Error in detect_faces: {e}")
            return np.array([])
    
    def detect_largest_face(self, image):
        """
        检测最大的人脸
        
        Returns:
            box: [x1, y1, x2, y2] or None
        """
        boxes = self.detect_faces(image)
        
        if len(boxes) == 0:
            return None
        
        # 计算面积，返回最大的
        areas = [(box[2] - box[0]) * (box[3] - box[1]) for box in boxes]
        largest_idx = np.argmax(areas)
        
        return boxes[largest_idx]
    
    def detect_and_align(self, image):
        """
        检测并对齐人脸
        
        Returns:
            list of aligned face images
        """
        if self.mtcnn is not None:
            try:
                faces = self.mtcnn(image)
                if faces is not None:
                    if faces.dim() == 3:
                        faces = faces.unsqueeze(0)
                    return faces
            except:
                pass
        
        # Fallback: 只检测边界框
        boxes = self.detect_faces(image)
        faces = []
        
        if isinstance(image, np.ndarray):
            image = Image.fromarray(image)
        
        for box in boxes:
            x1, y1, x2, y2 = map(int, box)
            face = image.crop((x1, y1, x2, y2))
            face = face.resize((160, 160))
            faces.append(face)
        
        return faces