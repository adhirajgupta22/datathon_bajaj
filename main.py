from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
from typing import Optional, List, Dict, Any
import logging
import time
from collections import defaultdict
from datetime import datetime

from document_processor import DocumentProcessor
from config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Medical Bill Extraction API",
    description="AI-powered extraction of structured data from medical bills with fraud detection",
    version="1.0.0"
)

app.state.start_time = time.time()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def track_requests(request: Request, call_next):
    start_time = time.time()
    
    response = await call_next(request)
    
    process_time = (time.time() - start_time) * 1000
    endpoint = f"{request.method} {request.url.path}"
    
    metrics["total_requests"] += 1
    metrics["total_latency"] += process_time
    metrics["request_latencies"].append(process_time)
    
    if len(metrics["request_latencies"]) > 1000:
        metrics["request_latencies"] = metrics["request_latencies"][-1000:]
    
    metrics["endpoint_stats"][endpoint]["count"] += 1
    metrics["endpoint_stats"][endpoint]["total_time"] += process_time
    
    if response.status_code >= 400:
        metrics["failed_requests"] += 1
        metrics["endpoint_stats"][endpoint]["errors"] += 1
    else:
        metrics["successful_requests"] += 1
    
    response.headers["X-Process-Time-Ms"] = str(round(process_time, 2))
    return response

processor = DocumentProcessor()

metrics = {
    "total_requests": 0,
    "successful_requests": 0,
    "failed_requests": 0,
    "total_latency": 0.0,
    "request_latencies": [],
    "endpoint_stats": defaultdict(lambda: {"count": 0, "total_time": 0.0, "errors": 0})
}


class ExtractionRequest(BaseModel):
    document: HttpUrl


class FraudDetectionRequest(BaseModel):
    document: HttpUrl


class HealthResponse(BaseModel):
    status: str
    llm_model: str
    uptime_seconds: float


class MetricsResponse(BaseModel):
    total_requests: int
    successful_requests: int
    failed_requests: int
    average_latency_ms: float
    min_latency_ms: float
    max_latency_ms: float
    p95_latency_ms: float
    endpoint_stats: Dict[str, Any]


@app.get("/", response_model=HealthResponse)
async def health_check():
    uptime = time.time() - app.state.start_time
    return HealthResponse(
        status="healthy",
        llm_model=settings.llm_model,
        uptime_seconds=round(uptime, 2)
    )


@app.post("/extract-bill-data")
async def extract_bill_data(request: ExtractionRequest):
    try:
        logger.info(f"Processing document: {request.document}")
        
        raw_result = processor.extract_data(str(request.document))
        
        total_items = sum(
            len(page['bill_items']) 
            for page in raw_result['pagewise_line_items']
        )
        
        reconciled_amount = 0.0
        for page in raw_result['pagewise_line_items']:
            for item in page['bill_items']:
                reconciled_amount += item.get('item_amount', 0.0)
        
        result = {
            "is_success": True,
            "data": {
                "pagewise_line_items": raw_result['pagewise_line_items'],
                "total_item_count": total_items,
                "reconciled_amount": round(reconciled_amount, 2)
            },
            "token_usage": raw_result['token_usage']
        }
        
        logger.info(f"Extraction completed. Items: {total_items}, Amount: {reconciled_amount}")
        return result
        
    except Exception as e:
        logger.error(f"Extraction failed: {str(e)}", exc_info=True)
        return {
            "is_success": False,
            "message": str(e)
        }


@app.post("/detect-fraud")
async def detect_fraud(request: FraudDetectionRequest):
    """Detect potential fraud in medical bills using AI-powered analysis.
    
    This endpoint analyzes bills for:
    - Font inconsistencies
    - Alignment issues
    - Suspicious alterations
    - Mathematical discrepancies
    - Duplicate items
    """
    try:
        logger.info(f"Fraud detection for document: {request.document}")
        
        fraud_result = processor.detect_fraud(str(request.document))
        
        result = {
            "is_success": True,
            "fraud_analysis": fraud_result,
            "timestamp": datetime.utcnow().isoformat()
        }
        
        logger.info(f"Fraud detection completed. Risk score: {fraud_result.get('overall_risk_score', 0)}")
        return result
        
    except Exception as e:
        logger.error(f"Fraud detection failed: {str(e)}", exc_info=True)
        return {
            "is_success": False,
            "message": str(e)
        }


@app.get("/metrics", response_model=MetricsResponse)
async def get_metrics():
    """Get API performance metrics and statistics."""
    latencies = metrics["request_latencies"]
    
    if latencies:
        sorted_latencies = sorted(latencies)
        avg_latency = sum(latencies) / len(latencies)
        min_latency = min(latencies)
        max_latency = max(latencies)
        p95_index = int(len(sorted_latencies) * 0.95)
        p95_latency = sorted_latencies[p95_index] if sorted_latencies else 0
    else:
        avg_latency = min_latency = max_latency = p95_latency = 0
    
    endpoint_stats_formatted = {}
    for endpoint, stats in metrics["endpoint_stats"].items():
        endpoint_stats_formatted[endpoint] = {
            "total_requests": stats["count"],
            "total_errors": stats["errors"],
            "avg_latency_ms": round(stats["total_time"] / stats["count"], 2) if stats["count"] > 0 else 0,
            "error_rate": round(stats["errors"] / stats["count"] * 100, 2) if stats["count"] > 0 else 0
        }
    
    return MetricsResponse(
        total_requests=metrics["total_requests"],
        successful_requests=metrics["successful_requests"],
        failed_requests=metrics["failed_requests"],
        average_latency_ms=round(avg_latency, 2),
        min_latency_ms=round(min_latency, 2),
        max_latency_ms=round(max_latency, 2),
        p95_latency_ms=round(p95_latency, 2),
        endpoint_stats=endpoint_stats_formatted
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=True
    )
