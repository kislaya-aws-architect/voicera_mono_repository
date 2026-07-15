import { NextRequest, NextResponse } from "next/server"

import { SERVER_API_URL } from "@/lib/api-config"

const API_BASE_URL = SERVER_API_URL

type RouteContext = { params: Promise<{ reportId: string }> }

// GET - fetch a single Sajag report, proxies to
// GET /api/v1/sajag/reports/{report_id} on the backend
export async function GET(request: NextRequest, context: RouteContext) {
  try {
    const authHeader = request.headers.get("Authorization")
    if (!authHeader) {
      return NextResponse.json(
        { error: "Authorization header is required" },
        { status: 401 }
      )
    }

    const { reportId } = await context.params
    const encodedReportId = encodeURIComponent(reportId)

    const response = await fetch(
      `${API_BASE_URL}/api/v1/sajag/reports/${encodedReportId}`,
      {
        method: "GET",
        headers: {
          Accept: "application/json",
          Authorization: authHeader,
        },
      }
    )

    const data = await response.json().catch(() => ({}))
    if (!response.ok) {
      return NextResponse.json(data, { status: response.status })
    }

    return NextResponse.json(data)
  } catch (error) {
    console.error("Error fetching Sajag report:", error)
    return NextResponse.json(
      { error: "Internal server error" },
      { status: 500 }
    )
  }
}
