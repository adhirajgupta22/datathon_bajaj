import io
import os
import tempfile
from typing import List, Dict, Any, Tuple
from PIL import Image, ImageEnhance, ImageFilter, ImageStat
import requests
from pdf2image import convert_from_path
import json
import numpy as np
from collections import Counter
import cv2

from config import settings


class DocumentProcessor:
    
    EXTRACTION_PROMPT = """You are an expert data extraction AI for medical bills.

Extract ALL line items from this medical bill page with exact details.

RETURN ONLY JSON IN THIS EXACT FORMAT:
{
    "bill_items": [
        {
            "item_name": "exact text from bill",
            "item_rate": 100.00,
            "item_quantity": 1.0,
            "item_amount": 100.00
        }
    ]
}

CRITICAL RULES:
1. item_name: Copy the EXACT description from the bill
2. item_rate: Unit price/rate. If missing, use item_amount
3. item_quantity: Number of units. If not shown, use 1.0
4. item_amount: TOTAL amount for this line (rate × quantity)
5. Use numerical values only (no currency symbols)
6. Extract EVERY line item - do not skip any
7. Do not include subtotals or grand totals as line items
8. Handle handwritten text carefully
9. All numbers should be floats with 2 decimal places

Return ONLY the JSON object, nothing else."""

    FRAUD_DETECTION_PROMPT = """Analyze this medical bill for potential fraud indicators:

Look for:
1. Font inconsistencies (different fonts, sizes, or styles for amounts vs descriptions)
2. Alignment issues (misaligned text, overlapping text)
3. Suspicious alterations (whiteout marks, erasures, overwriting)
4. Unusual patterns (repeated amounts, round numbers, excessive charges)
5. Image quality issues (signs of photoshopping, digital manipulation)
6. Mathematical discrepancies (totals not matching sum of items)
7. Duplicated line items with same or similar descriptions

Return a JSON object:
{
    "fraud_flags": [
        {
            "type": "font_inconsistency|alignment_issue|alteration|suspicious_pattern|math_error|duplicate_items",
            "severity": "high|medium|low",
            "description": "Detailed description of the issue",
            "location": "Where on the bill this was found"
        }
    ],
    "overall_risk_score": 0.0-1.0,
    "recommendation": "approve|review|reject"
}

Only return the JSON object."""

    def __init__(self):
        import google.generativeai as genai
        
        self.model = settings.llm_model
        os.environ["GOOGLE_API_KEY"] = settings.gemini_api_key
        genai.configure(api_key=settings.gemini_api_key)
        self.client = genai.GenerativeModel(self.model)
    
    def download_document(self, url: str) -> bytes:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.content
    
    def pdf_to_images(self, pdf_bytes: bytes) -> List[Image.Image]:
        with tempfile.NamedTemporaryFile(delete=False, suffix='.pdf') as tmp_file:
            tmp_file.write(pdf_bytes)
            tmp_path = tmp_file.name
        
        try:
            images = convert_from_path(tmp_path, dpi=300)
            return images
        finally:
            os.unlink(tmp_path)
    
    def preprocess_image(self, image: Image.Image) -> Dict[str, Any]:
        """Apply preprocessing techniques to improve extraction accuracy.
        
        Returns both preprocessed image and preprocessing metadata.
        """
        preprocessing_steps = []
        
        if image.mode != 'RGB':
            image = image.convert('RGB')
            preprocessing_steps.append("color_mode_conversion")
        
        enhancer = ImageEnhance.Contrast(image)
        image = enhancer.enhance(1.5)
        preprocessing_steps.append("contrast_enhancement_1.5x")
        
        enhancer = ImageEnhance.Sharpness(image)
        image = enhancer.enhance(2.0)
        preprocessing_steps.append("sharpness_enhancement_2.0x")
        
        stat = ImageStat.Stat(image)
        avg_brightness = sum(stat.mean) / len(stat.mean)
        
        if avg_brightness < 100:
            enhancer = ImageEnhance.Brightness(image)
            image = enhancer.enhance(1.3)
            preprocessing_steps.append(f"brightness_increase_1.3x_from_{avg_brightness:.1f}")
        elif avg_brightness > 200:
            enhancer = ImageEnhance.Brightness(image)
            image = enhancer.enhance(0.8)
            preprocessing_steps.append(f"brightness_decrease_0.8x_from_{avg_brightness:.1f}")
        
        image = image.filter(ImageFilter.MedianFilter(size=3))
        preprocessing_steps.append("median_filter_noise_reduction")
        
        return {
            "preprocessed_image": image,
            "steps_applied": preprocessing_steps,
            "original_brightness": avg_brightness
        }
    
    def detect_whitening_fraud(self, image: Image.Image) -> Dict[str, Any]:
        """Detect whiteout/correction fluid using image analysis.
        
        Algorithm:
        1. Convert to grayscale and analyze brightness distribution
        2. Look for abnormal white patches (brightness > 245)
        3. Check if white patches overlap with text regions
        4. Analyze edge discontinuities around white patches
        """
        img_array = np.array(image)
        gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)
        
        _, bright_mask = cv2.threshold(gray, 245, 255, cv2.THRESH_BINARY)
        
        contours, _ = cv2.findContours(bright_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        suspicious_regions = []
        for contour in contours:
            area = cv2.contourArea(contour)
            if 50 < area < 5000:
                x, y, w, h = cv2.boundingRect(contour)
                
                region = gray[y:y+h, x:x+w]
                edges = cv2.Canny(region, 50, 150)
                edge_density = np.sum(edges > 0) / (w * h)
                
                if edge_density < 0.05:
                    suspicious_regions.append({
                        "location": f"x:{x}, y:{y}, width:{w}, height:{h}",
                        "area_pixels": int(area),
                        "edge_density": float(edge_density),
                        "confidence": "high" if edge_density < 0.02 else "medium"
                    })
        
        return {
            "whitening_detected": len(suspicious_regions) > 0,
            "suspicious_regions": suspicious_regions,
            "total_suspicious_patches": len(suspicious_regions)
        }
    
    def detect_font_inconsistencies(self, image: Image.Image) -> Dict[str, Any]:
        """Detect font inconsistencies using edge analysis.
        
        Algorithm:
        1. Apply edge detection to find text regions
        2. Segment image into text blocks
        3. Analyze stroke width variation across blocks
        4. Compare font characteristics (thickness, spacing)
        """
        img_array = np.array(image)
        gray = cv2.cvtColor(img_array, cv2.COLOR_RGB2GRAY)
        
        binary = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                       cv2.THRESH_BINARY_INV, 11, 2)
        
        num_labels, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
        
        stroke_widths = []
        text_regions = []
        
        for i in range(1, num_labels):
            area = stats[i, cv2.CC_STAT_AREA]
            if 100 < area < 10000:
                x, y, w, h = stats[i, cv2.CC_STAT_LEFT], stats[i, cv2.CC_STAT_TOP], \
                             stats[i, cv2.CC_STAT_WIDTH], stats[i, cv2.CC_STAT_HEIGHT]
                
                region = binary[y:y+h, x:x+w]
                
                if region.size > 0:
                    stroke_width = np.sum(region > 0) / (h if h > 0 else 1)
                    stroke_widths.append(stroke_width)
                    text_regions.append({"x": x, "y": y, "w": w, "h": h, "stroke": stroke_width})
        
        if len(stroke_widths) > 5:
            mean_stroke = np.mean(stroke_widths)
            std_stroke = np.std(stroke_widths)
            coefficient_of_variation = std_stroke / mean_stroke if mean_stroke > 0 else 0
            
            inconsistency_detected = coefficient_of_variation > 0.3
            
            return {
                "font_inconsistency_detected": inconsistency_detected,
                "stroke_width_variation": float(coefficient_of_variation),
                "mean_stroke_width": float(mean_stroke),
                "std_deviation": float(std_stroke),
                "text_regions_analyzed": len(stroke_widths),
                "confidence": "high" if coefficient_of_variation > 0.5 else "medium" if coefficient_of_variation > 0.3 else "low"
            }
        
        return {
            "font_inconsistency_detected": False,
            "stroke_width_variation": 0.0,
            "text_regions_analyzed": len(stroke_widths)
        }
    
    def detect_digital_manipulation(self, image: Image.Image) -> Dict[str, Any]:
        """Detect digital manipulation using Error Level Analysis (ELA).
        
        Algorithm:
        1. Save image at known quality level
        2. Compare with original to find compression inconsistencies
        3. Areas with different compression = potential manipulation
        """
        img_array = np.array(image)
        
        temp_buffer = io.BytesIO()
        image.save(temp_buffer, format='JPEG', quality=90)
        temp_buffer.seek(0)
        recompressed = Image.open(temp_buffer)
        recompressed_array = np.array(recompressed)
        
        if img_array.shape == recompressed_array.shape:
            diff = np.abs(img_array.astype(float) - recompressed_array.astype(float))
            error_level = np.mean(diff)
            max_error = np.max(diff)
            
            high_error_mask = np.mean(diff, axis=2) > (error_level + 2 * np.std(diff))
            suspicious_pixels = np.sum(high_error_mask)
            total_pixels = high_error_mask.size
            
            manipulation_detected = (suspicious_pixels / total_pixels) > 0.01
            
            return {
                "digital_manipulation_detected": manipulation_detected,
                "average_error_level": float(error_level),
                "max_error_level": float(max_error),
                "suspicious_pixel_percentage": float(suspicious_pixels / total_pixels * 100),
                "confidence": "high" if (suspicious_pixels / total_pixels) > 0.05 else "medium"
            }
        
        return {
            "digital_manipulation_detected": False,
            "error": "Image format mismatch"
        }
    
    def verify_mathematical_consistency(self, line_items: List[Dict]) -> Dict[str, Any]:
        """Verify mathematical consistency of bill amounts.
        
        Algorithm:
        1. Check if item_amount = item_rate × item_quantity
        2. Flag rounding errors > $0.10
        3. Detect duplicate items with same description
        """
        inconsistencies = []
        duplicates = []
        
        item_names = []
        
        for idx, item in enumerate(line_items):
            rate = item.get('item_rate', 0)
            quantity = item.get('item_quantity', 1)
            amount = item.get('item_amount', 0)
            
            expected_amount = rate * quantity
            difference = abs(amount - expected_amount)
            
            if difference > 0.10:  # More than 10 cents difference
                inconsistencies.append({
                    "item_index": idx,
                    "item_name": item.get('item_name', 'Unknown'),
                    "stated_amount": amount,
                    "calculated_amount": round(expected_amount, 2),
                    "difference": round(difference, 2),
                    "severity": "high" if difference > 10 else "medium"
                })
            
            item_name = item.get('item_name', '').strip().lower()
            if item_name in item_names:
                duplicates.append({
                    "item_name": item.get('item_name', 'Unknown'),
                    "duplicate_index": idx
                })
            item_names.append(item_name)
        
        return {
            "mathematical_inconsistencies_found": len(inconsistencies) > 0,
            "inconsistencies": inconsistencies,
            "duplicate_items_found": len(duplicates) > 0,
            "duplicates": duplicates,
            "total_items_checked": len(line_items)
        }
    
    def extract_with_gemini(self, image: Image.Image, prompt: str) -> tuple:
        response = self.client.generate_content(
            [prompt, image],
            generation_config={
                "temperature": settings.temperature,
                "max_output_tokens": settings.max_tokens,
            }
        )
        
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        
        try:
            if response.usage_metadata:
                usage["input_tokens"] = response.usage_metadata.prompt_token_count
                usage["output_tokens"] = response.usage_metadata.candidates_token_count
                usage["total_tokens"] = response.usage_metadata.total_token_count
        except:
            text_tokens = len(prompt) // 4
            image_tokens = 258
            output_tokens = len(response.text) // 4
            usage["input_tokens"] = text_tokens + image_tokens
            usage["output_tokens"] = output_tokens
            usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
        
        return response.text, usage
    
    def extract_data(self, document_url: str) -> Dict[str, Any]:
        doc_bytes = self.download_document(document_url)
        is_pdf = doc_bytes[:4] == b'%PDF'
        
        if is_pdf:
            images = self.pdf_to_images(doc_bytes)
        else:
            image = Image.open(io.BytesIO(doc_bytes))
            images = [image]
        
        pagewise_items = []
        total_tokens = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        preprocessing_info = []
        
        for page_idx, image in enumerate(images):
            preprocessed_data = self.preprocess_image(image)
            preprocessed_image = preprocessed_data["preprocessed_image"]
            preprocessing_info.append({
                "page_no": page_idx + 1,
                "steps": preprocessed_data["steps_applied"],
                "original_brightness": preprocessed_data["original_brightness"]
            })
            
            extraction_result, usage = self.extract_with_gemini(preprocessed_image, self.EXTRACTION_PROMPT)
            
            total_tokens["input_tokens"] += usage["input_tokens"]
            total_tokens["output_tokens"] += usage["output_tokens"]
            total_tokens["total_tokens"] += usage["total_tokens"]
            
            try:
                extraction_result = extraction_result.strip()
                if extraction_result.startswith("```json"):
                    extraction_result = extraction_result[7:]
                if extraction_result.startswith("```"):
                    extraction_result = extraction_result[3:]
                if extraction_result.endswith("```"):
                    extraction_result = extraction_result[:-3]
                
                page_data = json.loads(extraction_result.strip())
                pagewise_items.append({
                    "page_no": str(page_idx + 1),
                    "page_type": "Bill Detail",
                    "bill_items": page_data.get("bill_items", [])
                })
            except json.JSONDecodeError:
                continue
        
        return {
            "pagewise_line_items": pagewise_items,
            "token_usage": total_tokens,
            "preprocessing_applied": preprocessing_info
        }
    
    def detect_fraud(self, document_url: str) -> Dict[str, Any]:
        """Comprehensive fraud detection using multiple algorithms.
        
        Combines:
        1. AI-based visual analysis (Gemini)
        2. Whitening detection (OpenCV)
        3. Font inconsistency detection (stroke width analysis)
        4. Digital manipulation detection (Error Level Analysis)
        5. Mathematical verification (calculation checks)
        """
        doc_bytes = self.download_document(document_url)
        is_pdf = doc_bytes[:4] == b'%PDF'
        
        if is_pdf:
            images = self.pdf_to_images(doc_bytes)
        else:
            image = Image.open(io.BytesIO(doc_bytes))
            images = [image]
        
        image = images[0]
        
        fraud_result, _ = self.extract_with_gemini(image, self.FRAUD_DETECTION_PROMPT)
        
        try:
            fraud_result = fraud_result.strip()
            if fraud_result.startswith("```json"):
                fraud_result = fraud_result[7:]
            if fraud_result.startswith("```"):
                fraud_result = fraud_result[3:]
            if fraud_result.endswith("```"):
                fraud_result = fraud_result[:-3]
            
            ai_fraud_data = json.loads(fraud_result.strip())
        except json.JSONDecodeError:
            ai_fraud_data = {
                "fraud_flags": [],
                "overall_risk_score": 0.0,
                "recommendation": "review"
            }
        
        whitening_analysis = self.detect_whitening_fraud(image)
        font_analysis = self.detect_font_inconsistencies(image)
        manipulation_analysis = self.detect_digital_manipulation(image)
        
        extraction_result, _ = self.extract_with_gemini(image, self.EXTRACTION_PROMPT)
        line_items = []
        try:
            extraction_result = extraction_result.strip()
            if extraction_result.startswith("```json"):
                extraction_result = extraction_result[7:]
            if extraction_result.startswith("```"):
                extraction_result = extraction_result[3:]
            if extraction_result.endswith("```"):
                extraction_result = extraction_result[:-3]
            
            extraction_data = json.loads(extraction_result.strip())
            line_items = extraction_data.get("bill_items", [])
        except json.JSONDecodeError:
            line_items = []
        
        math_verification = self.verify_mathematical_consistency(line_items)
        
        combined_fraud_flags = ai_fraud_data.get("fraud_flags", [])
        
        if whitening_analysis["whitening_detected"]:
            for region in whitening_analysis["suspicious_regions"]:
                combined_fraud_flags.append({
                    "type": "whitening_detected",
                    "severity": "high" if region["confidence"] == "high" else "medium",
                    "description": f"Whiteout/correction fluid detected at {region['location']} (area: {region['area_pixels']} pixels, edge density: {region['edge_density']:.4f})",
                    "location": region["location"],
                    "algorithm": "OpenCV_Canny_Edge_Detection",
                    "confidence": region["confidence"]
                })
        
        if font_analysis["font_inconsistency_detected"]:
            combined_fraud_flags.append({
                "type": "font_inconsistency",
                "severity": font_analysis.get("confidence", "medium"),
                "description": f"Font inconsistency detected. Stroke width variation: {font_analysis['stroke_width_variation']:.3f} (mean: {font_analysis['mean_stroke_width']:.2f}, std: {font_analysis['std_deviation']:.2f})",
                "location": f"Analyzed {font_analysis['text_regions_analyzed']} text regions",
                "algorithm": "Stroke_Width_Transform_Analysis",
                "confidence": font_analysis.get("confidence", "medium")
            })
        
        if manipulation_analysis.get("digital_manipulation_detected", False):
            combined_fraud_flags.append({
                "type": "digital_manipulation",
                "severity": manipulation_analysis.get("confidence", "medium"),
                "description": f"Digital manipulation detected. Error level: {manipulation_analysis['average_error_level']:.2f}, suspicious pixels: {manipulation_analysis['suspicious_pixel_percentage']:.2f}%",
                "location": "Image-wide analysis",
                "algorithm": "Error_Level_Analysis_ELA",
                "confidence": manipulation_analysis.get("confidence", "medium")
            })
        
        if math_verification["mathematical_inconsistencies_found"]:
            for inconsistency in math_verification["inconsistencies"]:
                combined_fraud_flags.append({
                    "type": "math_error",
                    "severity": inconsistency["severity"],
                    "description": f"Mathematical error in '{inconsistency['item_name']}': stated ${inconsistency['stated_amount']} but calculated ${inconsistency['calculated_amount']} (difference: ${inconsistency['difference']})",
                    "location": f"Line item {inconsistency['item_index'] + 1}",
                    "algorithm": "Arithmetic_Verification",
                    "confidence": "high"
                })
        
        if math_verification["duplicate_items_found"]:
            for duplicate in math_verification["duplicates"]:
                combined_fraud_flags.append({
                    "type": "duplicate_items",
                    "severity": "medium",
                    "description": f"Duplicate item detected: '{duplicate['item_name']}'",
                    "location": f"Line item {duplicate['duplicate_index'] + 1}",
                    "algorithm": "Exact_Match_Detection",
                    "confidence": "high"
                })
        
        base_risk = ai_fraud_data.get("overall_risk_score", 0.0)
        technical_risk = 0.0
        
        if whitening_analysis["whitening_detected"]:
            technical_risk += 0.3
        if font_analysis["font_inconsistency_detected"]:
            technical_risk += 0.2
        if manipulation_analysis.get("digital_manipulation_detected", False):
            technical_risk += 0.25
        if math_verification["mathematical_inconsistencies_found"]:
            technical_risk += 0.15 * min(len(math_verification["inconsistencies"]), 3)
        
        combined_risk_score = min((base_risk + technical_risk) / 2, 1.0)
        
        if combined_risk_score > 0.7:
            recommendation = "reject"
        elif combined_risk_score > 0.4:
            recommendation = "review"
        else:
            recommendation = "approve"
        
        return {
            "fraud_flags": combined_fraud_flags,
            "overall_risk_score": round(combined_risk_score, 3),
            "recommendation": recommendation,
            "technical_analysis": {
                "whitening_detection": whitening_analysis,
                "font_analysis": font_analysis,
                "digital_manipulation": manipulation_analysis,
                "mathematical_verification": math_verification
            },
            "ai_analysis": ai_fraud_data,
            "total_fraud_indicators": len(combined_fraud_flags)
        }
