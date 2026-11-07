"""Pre-EDA source-column contract; no modeling features."""

SHOP_COLUMN_TYPES = {
    'shop_id': 'TEXT PRIMARY KEY',
    'shop_name': 'TEXT NOT NULL',
    'city': 'TEXT NOT NULL',
    'region': 'TEXT',
    'opening_hour': 'TIME',
    'closing_hour': 'TIME',
    'price_min': 'INT CHECK (price_min >= 0)',
    'price_max': 'INT CHECK (price_max >= price_min)',
    'views': 'INT DEFAULT 0',
    'shop_review_count': 'INT DEFAULT 0',
    'shop_avg_rating': 'NUMERIC(3,1) CHECK (shop_avg_rating BETWEEN 0 AND 10)',
    'excellent_count': 'INT DEFAULT 0',
    'good_count': 'INT DEFAULT 0',
    'average_count': 'INT DEFAULT 0',
    'poor_count': 'INT DEFAULT 0',
    'location_score': 'NUMERIC(3,1)',
    'price_score': 'NUMERIC(3,1)',
    'quality_score': 'NUMERIC(3,1)',
    'service_score': 'NUMERIC(3,1)',
    'space_score': 'NUMERIC(3,1)',
    'last_crawled_at': 'TIMESTAMP NOT NULL DEFAULT now()',
    'shop_url': 'TEXT',  # Preserve the URL, as explicitly requested by the user.
}
USER_COLUMN_TYPES = {
    'user_pseudo_id': 'TEXT PRIMARY KEY',
    'user_name': 'TEXT NOT NULL',
    'first_seen_at': 'TIMESTAMP NOT NULL DEFAULT now()',
}
REVIEW_COLUMN_TYPES = {
    'review_id': 'BIGSERIAL PRIMARY KEY',
    'source_review_id': 'BIGINT',
    'shop_id': 'TEXT NOT NULL REFERENCES public.shops(shop_id)',
    'user_pseudo_id': 'TEXT NOT NULL REFERENCES public.users(user_pseudo_id)',
    'rating': 'NUMERIC(3,1) NOT NULL CHECK (rating BETWEEN 0 AND 10)',
    'device': 'TEXT',
    'comment_datetime': 'TIMESTAMP NOT NULL',
    'comment_text': 'TEXT NOT NULL',
    'num_photos': 'INT DEFAULT 0 CHECK (num_photos >= 0)',
    'source_platform': 'TEXT NOT NULL',
    'crawled_at': 'TIMESTAMP NOT NULL DEFAULT now()',
}
TABLE_COLUMN_TYPES = {'shops': SHOP_COLUMN_TYPES, 'users': USER_COLUMN_TYPES, 'reviews': REVIEW_COLUMN_TYPES}
SHOP_COLUMNS = tuple(SHOP_COLUMN_TYPES)
USER_COLUMNS = tuple(USER_COLUMN_TYPES)
REVIEW_COLUMNS = tuple(REVIEW_COLUMN_TYPES)
REVIEW_WRITE_COLUMNS = REVIEW_COLUMNS[1:]  # PostgreSQL allocates the surrogate key.
FLAT_COLUMNS = tuple(dict.fromkeys((*REVIEW_WRITE_COLUMNS, *USER_COLUMNS, *SHOP_COLUMNS)))
NATURAL_KEY = ('shop_id', 'user_pseudo_id', 'comment_datetime', 'comment_text')
RAW_FIELD_MAP = {
    'ID Quán': 'shop_id', 'Tên Quán': 'shop_name', 'Thành Phố': 'city',
    'URL Quán': 'shop_url', 'Giờ Mở Cửa': 'opening_hour', 'Giờ Đóng Cửa': 'closing_hour',
    'Giá Thấp Nhất': 'price_min', 'Giá Cao Nhất': 'price_max', 'Lượt Xem': 'views',
    'Tổng Số Bình Luận': 'shop_review_count', 'Điểm Trung Bình Quán': 'shop_avg_rating',
    'Số Bình Luận Tuyệt Vời': 'excellent_count', 'Số Bình Luận Khá Tốt': 'good_count',
    'Số Bình Luận Trung Bình': 'average_count', 'Số Bình Luận Kém': 'poor_count',
    'Điểm Vị Trí': 'location_score', 'Điểm Giá Cả': 'price_score',
    'Điểm Chất Lượng': 'quality_score', 'Điểm Phục Vụ': 'service_score',
    'Điểm Không Gian': 'space_score', 'Tên User': 'user_name',
    'ID Bình Luận': 'source_review_id', 'Điểm Đánh Giá': 'rating',
    'Thiết Bị': 'device', 'Ngày Giờ': 'comment_datetime', 'Bình Luận': 'comment_text',
    'Số Ảnh Bình Luận': 'num_photos',
}
RAW_FIELD_MAP.update({k.replace('Bình Luận', 'BL'): v for k, v in list(RAW_FIELD_MAP.items()) if k.startswith('Số Bình Luận')})
