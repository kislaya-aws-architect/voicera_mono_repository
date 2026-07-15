import { NextRequest, NextResponse } from "next/server"

import { SERVER_API_URL } from "@/lib/api-config"

const API_BASE_URL = SERVER_API_URL

// GET - list Sajag reports (optional status_filter), proxies to
// GET /api/v1/sajag/reports on the backend (voicera_backend/app/routers/glific.py)
export async function GET(request: NextRequest) {
  try {
    const authHeader = request.headers.get("Authorization")
    if (!authHeader) {
      return NextResponse.json(
        { error: "Authorization header is required" },
        { status: 401 }
      )
    }

    const { searchParams } = new URL(request.url)
    const statusFilter = searchParams.get("status_filter")
    const limit = searchParams.get("limit")

    const params = new URLSearchParams()
    if (statusFilter) params.set("status_filter", statusFilter)
    if (limit) params.set("limit", limit)
    const query = params.toString() ? `?${params.toString()}` : ""

    const response = await fetch(`${API_BASE_URL}/api/v1/sajag/reports${query}`, {
      method: "GET",
      headers: {
        Accept: "application/json",
        Authorization: authHeader,
      },
    })

    const data = await response.json().catch(() => ({}))
    if (!response.ok) {
      return NextResponse.json(data, { status: response.status })
    }

    return NextResponse.json(data)
  } catch (error) {
    console.error("Error fetching Sajag reports:", error)
    return NextResponse.json(
      { error: "Internal server error" },
      { status: 500 }
    )
  }
}
