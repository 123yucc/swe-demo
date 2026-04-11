import os
import json
import numpy as np
from PIL import Image
import pickle
from datetime import datetime
import uuid


class FaceDatabase:
    """人脸数据库管理类"""
    
    def __init__(self, db_path='data/face_database'):
        self.db_path = db_path
        self.embeddings_file = os.path.join(db_path, 'embeddings.pkl')
        self.metadata_file = os.path.join(db_path, 'metadata.json')
        self.images_dir = os.path.join(db_path, 'images')
        
        # 创建目录
        os.makedirs(self.db_path, exist_ok=True)
        os.makedirs(self.images_dir, exist_ok=True)
        
        # 加载数据库
        self.embeddings = {}  # {face_id: embedding_vector}
        self.metadata = {}    # {face_id: {name, timestamp, image_path, ...}}
        self._load_database()
    
    def _load_database(self):
        """从磁盘加载数据库"""
        try:
            if os.path.exists(self.embeddings_file):
                with open(self.embeddings_file, 'rb') as f:
                    self.embeddings = pickle.load(f)
            
            if os.path.exists(self.metadata_file):
                with open(self.metadata_file, 'r') as f:
                    self.metadata = json.load(f)
            
            print(f"Database loaded: {len(self.embeddings)} faces")
            
        except Exception as e:
            print(f"Error loading database: {e}")
            self.embeddings = {}
            self.metadata = {}
    
    def _save_database(self):
        """保存数据库到磁盘"""
        try:
            with open(self.embeddings_file, 'wb') as f:
                pickle.dump(self.embeddings, f)
            
            with open(self.metadata_file, 'w') as f:
                json.dump(self.metadata, f, indent=2)
            
        except Exception as e:
            print(f"Error saving database: {e}")
    
    def add_face(self, name, embedding, face_image=None):
        """
        添加人脸到数据库
        
        Args:
            name: 人名
            embedding: 特征向量 (numpy array)
            face_image: PIL Image (optional)
        
        Returns:
            face_id: 唯一标识符
        """
        face_id = str(uuid.uuid4())
        
        # 保存特征向量
        self.embeddings[face_id] = embedding
        
        # 保存图像
        image_path = None
        if face_image is not None:
            image_filename = f"{face_id}.jpg"
            image_path = os.path.join(self.images_dir, image_filename)
            
            if isinstance(face_image, np.ndarray):
                face_image = Image.fromarray(face_image)
            
            face_image.save(image_path)
        
        # 保存元数据
        self.metadata[face_id] = {
            'name': name,
            'timestamp': datetime.now().isoformat(),
            'image_path': image_path,
            'embedding_shape': embedding.shape
        }
        
        # 持久化
        self._save_database()
        
        return face_id
    
    def search_face(self, embedding, threshold=0.6):
        """
        在数据库中搜索最相似的人脸
        """
        if len(self.embeddings) == 0:
            return None
        
        best_match = None
        best_similarity = -1
        
        for face_id, db_embedding in self.embeddings.items():
            # 计算余弦相似度
            similarity = np.dot(embedding, db_embedding)
            
            if similarity > best_similarity:
                best_similarity = similarity
                best_match = face_id
        
        # 检查是否超过阈值
        if best_similarity >= threshold:
            result = {
                'face_id': best_match,
                'name': self.metadata[best_match]['name'],
                'similarity': float(best_similarity),
                'metadata': self.metadata[best_match]
            }
            
            # --- 修复部分开始：加载图片对象返回给 app.py ---
            image_path = self.metadata[best_match].get('image_path')
            if image_path and os.path.exists(image_path):
                try:
                    # 打开图片并转换为RGB，方便 image_processor 处理
                    img = Image.open(image_path).convert('RGB')
                    result['image'] = img  # app.py 需要这个 key
                except Exception as e:
                    print(f"Error loading face image from disk: {e}")
                    result['image'] = None
            else:
                result['image'] = None
            # --- 修复部分结束 ---
            
            return result
        else:
            return None
    
    def search_top_k(self, embedding, k=5, threshold=0.0):
        """
        返回最相似的k个人脸
        
        Returns:
            list of dicts
        """
        if len(self.embeddings) == 0:
            return []
        
        results = []
        
        for face_id, db_embedding in self.embeddings.items():
            similarity = np.dot(embedding, db_embedding)
            
            if similarity >= threshold:
                results.append({
                    'face_id': face_id,
                    'name': self.metadata[face_id]['name'],
                    'similarity': float(similarity),
                    'metadata': self.metadata[face_id]
                })
        
        # 按相似度排序
        results.sort(key=lambda x: x['similarity'], reverse=True)
        
        return results[:k]
    
    def delete_face(self, face_id):
        """删除指定人脸"""
        if face_id not in self.embeddings:
            return False
        
        # 删除图像文件
        if face_id in self.metadata and self.metadata[face_id].get('image_path'):
            image_path = self.metadata[face_id]['image_path']
            if os.path.exists(image_path):
                os.remove(image_path)
        
        # 删除数据
        del self.embeddings[face_id]
        del self.metadata[face_id]
        
        # 持久化
        self._save_database()
        
        return True
    
    def update_name(self, face_id, new_name):
        """更新人脸名称"""
        if face_id not in self.metadata:
            return False
        
        self.metadata[face_id]['name'] = new_name
        self.metadata[face_id]['updated_at'] = datetime.now().isoformat()
        
        self._save_database()
        return True
    
    def list_all_faces(self):
        """列出所有人脸"""
        faces = []
        
        for face_id, meta in self.metadata.items():
            face_info = {
                'face_id': face_id,
                'name': meta['name'],
                'timestamp': meta['timestamp'],
                'has_image': meta.get('image_path') is not None
            }
            
            # 如果有图像，转为base64
            if meta.get('image_path') and os.path.exists(meta['image_path']):
                try:
                    import base64
                    with open(meta['image_path'], 'rb') as f:
                        image_data = base64.b64encode(f.read()).decode('utf-8')
                        face_info['image'] = f"data:image/jpeg;base64,{image_data}"
                except:
                    pass
            
            faces.append(face_info)
        
        return faces
    
    def get_face_by_name(self, name):
        """根据名字查找所有人脸"""
        results = []
        
        for face_id, meta in self.metadata.items():
            if meta['name'].lower() == name.lower():
                results.append({
                    'face_id': face_id,
                    'metadata': meta,
                    'embedding': self.embeddings[face_id]
                })
        
        return results
    
    def clear_all(self):
        """清空整个数据库"""
        # 删除所有图像
        for face_id in list(self.metadata.keys()):
            self.delete_face(face_id)
        
        self.embeddings = {}
        self.metadata = {}
        self._save_database()
    
    def get_size(self):
        """获取数据库大小"""
        return len(self.embeddings)
    
    def get_statistics(self):
        """获取数据库统计信息"""
        names = [meta['name'] for meta in self.metadata.values()]
        unique_names = set(names)
        
        return {
            'total_faces': len(self.embeddings),
            'unique_people': len(unique_names),
            'name_distribution': {name: names.count(name) for name in unique_names}
        }